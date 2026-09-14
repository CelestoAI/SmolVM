import assert from "node:assert/strict";
import test from "node:test";
import { ActionBroker, MAX_BROWSER_PROGRAM_BYTES } from "../server/broker.js";
import { ConversationManager } from "../server/manager.js";
import type { ConversationContext } from "../server/types.js";

function harness() {
  const programs: string[] = [];
  const events: string[] = [];
  const context = {
    id: "conversation-test",
    stateVersion: 1,
    controlOwner: "agent",
    controlEpoch: "agent-control-test",
    runState: "idle",
    sessionLifecycle: "ready",
    messages: [],
    events: [],
    grants: [],
    cart: [],
    commerceRevision: 0,
    observationId: "",
    lastActivityAt: Date.now(),
    receipts: new Map(),
    browserSession: {
      sessionId: "browser-test",
      sandboxId: "vm-test",
      status: "ready",
      cdpUrl: "http://127.0.0.1:9222",
      async exec(command: string | readonly string[]) {
        const encoded = Array.isArray(command) ? command[1] : "";
        programs.push(Buffer.from(encoded, "base64url").toString("utf8"));
        return {
          ok: true, exitCode: 0,
          stdout: 'SMOLVM_BROWSER_RESULT={"ok":true,"value":{"programResult":{"title":"Example Domain"},"page":{"title":"Example Domain","url":"https://example.com"}}}\n',
          stderr: "", durationMs: 1,
        };
      },
      async delete() {},
    },
  } as ConversationContext;
  const broker = new ActionBroker(context, async () => undefined, (type) => events.push(type));
  return { broker, context, programs, events };
}

test("read-only browser programs wait for one-time approval", async () => {
  const { broker, programs, events } = harness();

  const pending = await broker.runProgram("return { title: await page.title() };", false, "Read the title");

  assert.equal(pending.approvalRequired, true);
  assert.equal(programs.length, 0);
  assert.deepEqual(events, ["approval.requested"]);
});

test("active browser programs wait for one-time approval", async () => {
  const { broker, context, programs } = harness();

  const pending = await broker.runProgram("await page.getByRole('link').click();", true, "Open the selected link");

  assert.equal(pending.approvalRequired, true);
  assert.equal(context.runState, "waiting_for_approval");
  assert.equal(programs.length, 0);
  assert.ok(context.pendingApproval);

  const outcome = await broker.resolveApproval(
    context.pendingApproval.approvalId,
    context.pendingApproval.actionDigest,
    true,
  );

  assert.deepEqual(outcome, {
    resumeAgent: true,
    browserResult: {
      programResult: { title: "Example Domain" },
      page: { title: "Example Domain", url: "https://example.com" },
    },
  });
  assert.equal(programs.length, 1);
  assert.doesNotMatch(programs[0], /visibleText/);
  assert.match(programs[0], /parsedUrl\.origin.*parsedUrl\.pathname/);
  assert.equal(context.pendingApproval, undefined);
});

test("navigation programs also require approval", async () => {
  const { broker, context, programs } = harness();

  const pending = await broker.runProgram(
    "await page.goto('https://www.amazon.in'); return { title: await page.title() };",
    true,
    "Open Amazon India and read its title",
  );

  assert.equal(pending.approvalRequired, true);
  assert.equal(programs.length, 0);
  await broker.resolveApproval(
    context.pendingApproval!.approvalId,
    context.pendingApproval!.actionDigest,
    true,
  );
  assert.equal(programs.length, 1);
});

test("browser programs return the current page when generated automation stops early", async () => {
  const { broker, context, programs, events } = harness();
  let executions = 0;
  context.browserSession!.exec = async (command: string | readonly string[]) => {
    const encoded = Array.isArray(command) ? command[1] : "";
    programs.push(Buffer.from(encoded, "base64url").toString("utf8"));
    executions += 1;
    if (executions === 1) {
      return {
        ok: false,
        exitCode: 1,
        stdout: "",
        stderr: "Locator wait exceeded the per-operation limit.",
        durationMs: 10_000,
      };
    }
    return {
      ok: true,
      exitCode: 0,
      stdout: `SMOLVM_BROWSER_RESULT=${JSON.stringify({
        ok: true,
        value: {
          title: "Amazon.com : iPhone",
          url: "https://www.amazon.com/s",
          visibleText: "Results iPhone $799.00 $899.00",
        },
      })}\n`,
      stderr: "",
      durationMs: 20_000,
    };
  };

  await broker.runProgram(
    "await page.goto('https://www.amazon.com'); await page.waitForSelector('#missing'); return [];",
    false,
    "Search Amazon for iPhone prices",
    true,
  );
  const outcome = await broker.resolveApproval(
    context.pendingApproval!.approvalId,
    context.pendingApproval!.actionDigest,
    true,
  );

  assert.match(programs[0], /setDefaultTimeout\(10_000\)/);
  assert.match(programs[0], /setDefaultNavigationTimeout\(15_000\)/);
  assert.doesNotMatch(programs[0], /Promise\.race/);
  assert.doesNotMatch(programs[0], /visibleText/);
  assert.match(programs[1], /innerText\(\{ timeout: 5_000 \}\)/);
  assert.match(programs[1], /pages\.find/);
  assert.match(programs[1], /parsedUrl\.origin.*parsedUrl\.pathname/);
  assert.match(programs[1], /primary\.first\(\)/);
  assert.doesNotMatch(programs[1], /page\.locator\('body'\)/);
  assert.match(programs[1], /account\|auth\|billing\|checkout/);
  assert.match(programs[1], /\[email redacted\]/);
  assert.deepEqual(outcome, {
    resumeAgent: true,
    browserResult: {
      completed: false,
      programResult: null,
      programError: "Locator wait exceeded the per-operation limit.",
      page: {
        title: "Amazon.com : iPhone",
        url: "https://www.amazon.com/s",
        visibleText: "Results iPhone $799.00 $899.00",
      },
    },
  });
  assert.equal(events.at(-2), "tool.completed");
  assert.equal(events.at(-1), "approval.resolved");
  assert.equal(context.runState, "idle");
});

test("browser programs reject empty, oversized, failed, malformed, and empty runner results", async () => {
  const { broker, context } = harness();

  await assert.rejects(() => broker.runProgram(" ", false, "Empty"), /1 to 18,000 bytes/);
  await assert.rejects(
    () => broker.runProgram("x".repeat(MAX_BROWSER_PROGRAM_BYTES + 1), false, "Oversized"),
    /1 to 18,000 bytes/,
  );

  context.browserSession!.exec = async () => ({
    ok: false, exitCode: 1, stdout: "", stderr: "page crashed", durationMs: 1,
  });
  await broker.runProgram("return true;", false, "Failing runner");
  const originalError = console.error;
  console.error = () => undefined;
  try {
    await assert.rejects(
      () => broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true),
      (error: unknown) => (error as { status?: number }).status === 422
        && (error as Error).message.includes("retry using the current page")
        && (error as { cause?: Error }).cause?.message === "page crashed",
    );

    context.browserSession!.exec = async () => ({
      ok: true, exitCode: 0, stdout: "unexpected output", stderr: "", durationMs: 1,
    });
    await broker.runProgram("return true;", false, "Malformed runner");
    await assert.rejects(
      () => broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true),
      (error: unknown) => (error as { status?: number }).status === 422
        && (error as { cause?: Error }).cause?.message === "The browser runner returned an invalid result.",
    );

    context.browserSession!.exec = async () => ({
      ok: true, exitCode: 0, stdout: "SMOLVM_BROWSER_RESULT={not-json}\n", stderr: "", durationMs: 1,
    });
    await broker.runProgram("return true;", false, "Invalid JSON runner result");
    await assert.rejects(
      () => broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true),
      (error: unknown) => (error as { status?: number }).status === 422
        && (error as { cause?: Error }).cause instanceof SyntaxError,
    );

    context.browserSession!.exec = async () => ({
      ok: true, exitCode: 0, stdout: 'SMOLVM_BROWSER_RESULT={"ok":false}\n', stderr: "", durationMs: 1,
    });
    await broker.runProgram("return true;", false, "Unsuccessful runner result");
    await assert.rejects(
      () => broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true),
      (error: unknown) => (error as { status?: number }).status === 422
        && (error as { cause?: Error }).cause?.message === "The browser runner returned an unsuccessful result.",
    );

    let fallbackExecutions = 0;
    context.browserSession!.exec = async () => {
      fallbackExecutions += 1;
      return fallbackExecutions === 1
        ? { ok: false, exitCode: 1, stdout: "", stderr: "locator timed out", durationMs: 1 }
        : {
            ok: true, exitCode: 0,
            stdout: 'SMOLVM_BROWSER_RESULT={"ok":true,"value":{"title":"","url":"about:blank","visibleText":""}}\n',
            stderr: "", durationMs: 1,
          };
    };
    await broker.runProgram("return true;", false, "No useful page fallback", true);
    await assert.rejects(
      () => broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true),
      (error: unknown) => (error as { status?: number }).status === 422
        && (error as { cause?: Error }).cause?.message === "locator timed out",
    );

    context.browserSession!.exec = async () => ({
      ok: true, exitCode: 0,
      stdout: 'SMOLVM_BROWSER_RESULT={"ok":true,"value":{"programResult":"undefined","page":{"title":"","url":"about:blank","visibleText":""}}}\n',
      stderr: "", durationMs: 1,
    });
    await broker.runProgram("async function unused() { return true; }", false, "No-op runner result");
    await assert.rejects(
      () => broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true),
      (error: unknown) => (error as { status?: number }).status === 422
        && (error as { cause?: Error }).cause?.message === "The Playwright program finished without returning data.",
    );
  } finally {
    console.error = originalError;
  }
});

test("messages are rejected while the user controls the browser", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const context = (manager as unknown as { context: ConversationContext }).context;
  context.controlOwner = "pause_requested";

  assert.throws(
    () => manager.send(created.id, "try again"),
    (error: unknown) => (error as { status?: number }).status === 409
      && (error as Error).message === "Wait for browser control to finish transferring, then send the message again.",
  );

  context.controlOwner = "agent";
  await manager.takeover(created.id);

  assert.throws(
    () => manager.send(created.id, "try again"),
    (error: unknown) => (error as { status?: number }).status === 409
      && (error as Error).message === "Select Return control before sending a message to OpenMuse.",
  );
  assert.deepEqual(manager.snapshot(created.id).messages, []);
  await manager.stop(created.id);
});

test("website approvals can be denied and stale approvals cannot execute", async () => {
  const denied = harness();
  await denied.broker.runProgram("await page.locator('button').click();", true, "Click once");
  const pending = denied.context.pendingApproval!;

  assert.deepEqual(
    await denied.broker.resolveApproval(pending.approvalId, pending.actionDigest, false),
    { resumeAgent: false },
  );
  assert.equal(denied.programs.length, 0);
  assert.equal(denied.context.runState, "idle");

  const stale = harness();
  await stale.broker.runProgram("await page.locator('button').click();", true, "Click once");
  stale.context.pendingApproval!.expiresAt = new Date(Date.now() - 1).toISOString();
  await assert.rejects(
    () => stale.broker.resolveApproval(
      stale.context.pendingApproval!.approvalId,
      stale.context.pendingApproval!.actionDigest,
      true,
    ),
    (error: unknown) => (error as { status?: number }).status === 409,
  );
  assert.equal(stale.programs.length, 0);
});
