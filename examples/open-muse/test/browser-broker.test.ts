import assert from "node:assert/strict";
import test from "node:test";
import { ActionBroker, MAX_BROWSER_PROGRAM_BYTES } from "../server/broker.js";
import { operationProgram } from "../server/browser-operations.js";
import { ConversationManager } from "../server/manager.js";
import type { ConversationContext } from "../server/types.js";

function harness() {
  const programs: string[] = [];
  const events: string[] = [];
  const eventPayloads: Record<string, unknown>[] = [];
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
    computer: {
      sessionId: "browser-test",
      sandboxId: "vm-test",
      status: "ready",
      cdpUrl: "http://127.0.0.1:9222",
      async exec(command: string | readonly string[]) {
        const encoded = Array.isArray(command) ? command[1] : "";
        const program = Buffer.from(encoded, "base64url").toString("utf8");
        programs.push(program);
        const programResult = program.includes("pageBindingRawUrl")
          ? { binding: "https://example.com", display: "https://example.com" }
          : { title: "Example Domain" };
        return {
          ok: true, exitCode: 0,
          stdout: `SMOLVM_BROWSER_RESULT=${JSON.stringify({ ok: true, value: { programResult, page: { title: "Example Domain", url: "https://example.com" } } })}\n`,
          stderr: "", durationMs: 1,
        };
      },
      async delete() {},
    },
  } as ConversationContext;
  const broker = new ActionBroker(context, async () => undefined, (type, payload) => { events.push(type); eventPayloads.push(payload); });
  return { broker, context, programs, events, eventPayloads };
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

test("passive browser operations run without approval", async () => {
  const { broker, context, programs, events } = harness();

  const observed = await broker.runWebOperation({ kind: "observe" });
  await broker.runWebOperation({ kind: "scroll", direction: "down" });

  assert.equal(context.pendingApproval, undefined);
  assert.equal(programs.length, 2);
  assert.match(programs[0], /main, \[role=main\]/);
  assert.match(programs[0], /\[email redacted\]/);
  assert.doesNotMatch(programs[0], /locator\('body'\)/);
  assert.match(programs[1], /mouse\.wheel\(0, 600\)/);
  assert.deepEqual(observed.page, { title: "Example Domain", url: "https://example.com" });
  assert.deepEqual(events, ["tool.started", "tool.completed", "tool.started", "tool.completed"]);
});

test("active browser operations create page-bound one-shot approvals", async () => {
  const { broker, context, programs, events } = harness();

  const pending = await broker.runWebOperation({
    kind: "click",
    target: { role: "button", name: "Add to cart" },
  });

  assert.equal(pending.approvalRequired, true);
  assert.equal(pending.pageUrl, "https://example.com");
  assert.equal("pageBinding" in pending, false);
  assert.deepEqual(pending.operation, { kind: "click", target: { role: "button", name: "Add to cart" } });
  assert.equal(programs.length, 1);
  assert.equal(context.runState, "waiting_for_approval");
  assert.equal(events.at(-1), "approval.requested");

  const approval = context.pendingApproval!;
  const outcome = await broker.resolveApproval(approval.approvalId, approval.actionDigest, true);

  assert.equal(programs.length, 2);
  assert.match(programs[1], /currentPage !== "https:\/\/example\.com"/);
  assert.match(programs[1], /getByRole\("button", \{ name: "Add to cart", exact: true \}\)/);
  assert.match(programs[1], /target\.count\(\) !== 1/);
  assert.equal(context.pendingApproval, undefined);
  assert.equal(context.runState, "idle");
  assert.equal("browserResult" in outcome, true);
});

test("operation programs serialize model fields as data", () => {
  const program = operationProgram({
    kind: "fill",
    target: { role: "textbox", name: "Name\"; process.exit(1); //" },
    value: "hello\nworld\"; throw new Error('injected'); //",
  }, "https://example.com/form");

  assert.match(program, /getByRole\("textbox"/);
  assert.match(program, /Name\\\"; process\.exit/);
  assert.match(program, /hello\\nworld\\\"; throw/);
  assert.doesNotMatch(program, /name: "Name"; process/);
  assert.match(program, /fieldSafety\.type === 'password'/);
  assert.match(program, /cc-\|current-password\|new-password\|one-time-code/);
  assert.match(operationProgram({ kind: "click", target: { role: "button", name: "Open" } }, "about:blank"), /: currentRawUrl/);
});

test("operation policy rejects private navigation and sensitive fields", async () => {
  const { broker, programs } = harness();

  await assert.rejects(() => broker.runWebOperation({ kind: "navigate", url: "http://127.0.0.1/admin" }), /private or local/);
  await assert.rejects(() => broker.runWebOperation({ kind: "navigate", url: "http://169.254.169.254/latest/meta-data" }), /private or local/);
  await assert.rejects(() => broker.runWebOperation({ kind: "navigate", url: "http://[::1]/admin" }), /private or local/);
  await assert.rejects(() => broker.runWebOperation({ kind: "navigate", url: "file:///etc/passwd" }), /public HTTP or HTTPS/);
  await assert.rejects(() => broker.runWebOperation({ kind: "fill", target: { role: "textbox", name: "Password" }, value: "secret" }), /Take control/);
  await assert.rejects(() => broker.runWebOperation({ kind: "click", target: { role: "button", name: 42 } } as never), /supported role/);
  await assert.rejects(() => broker.runWebOperation({ kind: "select", target: { role: "combobox", name: "Size" } } as never), /option label/);
  assert.equal(programs.length, 0);
});

test("operation approvals bind URL queries without exposing them in the approval card", async () => {
  const { broker, context, programs } = harness();
  context.computer!.exec = async (command: string | readonly string[]) => {
    const encoded = Array.isArray(command) ? command[1] : "";
    const program = Buffer.from(encoded, "base64url").toString("utf8");
    programs.push(program);
    const programResult = program.includes("pageBindingRawUrl")
      ? { binding: "https://example.com/search?q=private#results", display: "https://example.com/search" }
      : { clicked: true };
    return {
      ok: true, exitCode: 0,
      stdout: `SMOLVM_BROWSER_RESULT=${JSON.stringify({ ok: true, value: { programResult, page: { title: "Search", url: "https://example.com/search" } } })}\n`,
      stderr: "", durationMs: 1,
    };
  };

  const pending = await broker.runWebOperation({ kind: "click", target: { role: "button", name: "Open" } });

  assert.equal(pending.pageUrl, "https://example.com/search");
  assert.equal(JSON.stringify(pending).includes("q=private"), false);
  assert.equal(context.pendingApproval?.pageBinding, "https://example.com/search?q=private#results");

  await broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true);
  assert.match(programs[1], /https:\/\/example\.com\/search\?q=private#results/);
});

test("concurrent active operations share the first pending approval", async () => {
  const { broker, context } = harness();
  const originalExec = context.computer!.exec.bind(context.computer);
  context.computer!.exec = async (command: string | readonly string[], options?: { timeoutMs?: number }) => {
    await new Promise((resolve) => setTimeout(resolve, 5));
    return originalExec(command, options);
  };

  const [first, second] = await Promise.all([
    broker.runWebOperation({ kind: "click", target: { role: "button", name: "First" } }),
    broker.runWebOperation({ kind: "click", target: { role: "button", name: "Second" } }),
  ]);

  assert.equal(first.approvalId, second.approvalId);
  assert.deepEqual(context.pendingApproval?.operation, { kind: "click", target: { role: "button", name: "First" } });
});

test("approval events do not retain browser field values", async () => {
  const { broker, eventPayloads } = harness();

  const pending = await broker.runWebOperation({
    kind: "fill",
    target: { role: "textbox", name: "Email" },
    value: "person@example.com",
  });

  assert.equal(JSON.stringify(pending).includes("person@example.com"), true);
  assert.equal(JSON.stringify(eventPayloads.at(-1)).includes("person@example.com"), false);
});

test("conversation snapshots hide the private page binding", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const context = (manager as unknown as { context: ConversationContext }).context;
  context.pendingApproval = {
    kind: "browser_operation",
    approvalId: "approval-test",
    actionDigest: "digest-test",
    reason: "Click button “Open”",
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    operation: { kind: "click", target: { role: "button", name: "Open" } },
    pageUrl: "https://example.com/search",
    pageBinding: "https://example.com/search?q=private#results",
  };

  const snapshot = manager.snapshot(created.id);

  assert.equal(snapshot.pendingApproval?.pageUrl, "https://example.com/search");
  assert.deepEqual(snapshot.pendingApproval?.operation, context.pendingApproval.operation);
  assert.equal("pageBinding" in snapshot.pendingApproval!, false);
  assert.equal(JSON.stringify(snapshot).includes("q=private"), false);
});

test("browser programs return the current page when generated automation stops early", async () => {
  const { broker, context, programs, events } = harness();
  let executions = 0;
  context.computer!.exec = async (command: string | readonly string[]) => {
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

  context.computer!.exec = async () => ({
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

    context.computer!.exec = async () => ({
      ok: true, exitCode: 0, stdout: "unexpected output", stderr: "", durationMs: 1,
    });
    await broker.runProgram("return true;", false, "Malformed runner");
    await assert.rejects(
      () => broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true),
      (error: unknown) => (error as { status?: number }).status === 422
        && (error as { cause?: Error }).cause?.message === "The browser runner returned an invalid result.",
    );

    context.computer!.exec = async () => ({
      ok: true, exitCode: 0, stdout: "SMOLVM_BROWSER_RESULT={not-json}\n", stderr: "", durationMs: 1,
    });
    await broker.runProgram("return true;", false, "Invalid JSON runner result");
    await assert.rejects(
      () => broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true),
      (error: unknown) => (error as { status?: number }).status === 422
        && (error as { cause?: Error }).cause instanceof SyntaxError,
    );

    context.computer!.exec = async () => ({
      ok: true, exitCode: 0, stdout: 'SMOLVM_BROWSER_RESULT={"ok":false}\n', stderr: "", durationMs: 1,
    });
    await broker.runProgram("return true;", false, "Unsuccessful runner result");
    await assert.rejects(
      () => broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true),
      (error: unknown) => (error as { status?: number }).status === 422
        && (error as { cause?: Error }).cause?.message === "The browser runner returned an unsuccessful result.",
    );

    let fallbackExecutions = 0;
    context.computer!.exec = async () => {
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

    context.computer!.exec = async () => ({
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

  await assert.rejects(
    manager.send(created.id, "try again"),
    (error: unknown) => (error as { status?: number }).status === 409
      && (error as Error).message === "Wait for browser control to finish transferring, then send the message again.",
  );

  context.controlOwner = "agent";
  await manager.takeover(created.id);

  await assert.rejects(
    manager.send(created.id, "try again"),
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
