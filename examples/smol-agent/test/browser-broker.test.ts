import assert from "node:assert/strict";
import test from "node:test";
import { ActionBroker } from "../server/broker.js";
import type { ConversationContext } from "../server/types.js";

function harness() {
  const programs: string[] = [];
  const events: string[] = [];
  const context = {
    id: "conversation-test",
    stateVersion: 1,
    controlOwner: "agent",
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
        return { ok: true, exitCode: 0, stdout: 'SMOLVM_BROWSER_RESULT={"ok":true,"value":{"title":"Example Domain"}}\n', stderr: "", durationMs: 1 };
      },
      async delete() {},
    },
  } as ConversationContext;
  const broker = new ActionBroker(context, async () => undefined, (type) => events.push(type));
  return { broker, context, programs, events };
}

test("read-only browser programs execute immediately inside the browser session", async () => {
  const { broker, programs, events } = harness();

  const result = await broker.runProgram("return { title: await page.title() };", false, "Read the title");

  assert.deepEqual(result, { completed: true, result: { title: "Example Domain" } });
  assert.deepEqual(programs, ["return { title: await page.title() };"]);
  assert.deepEqual(events, ["tool.started", "tool.completed"]);
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

  assert.deepEqual(outcome, { resumeAgent: true });
  assert.equal(programs.length, 1);
  assert.equal(context.pendingApproval, undefined);
});

test("navigation executes immediately even when the model over-classifies it", async () => {
  const { broker, programs } = harness();

  const result = await broker.runProgram(
    "await page.goto('https://www.amazon.in'); return { title: await page.title() };",
    true,
    "Open Amazon India and read its title",
  );

  assert.deepEqual(result, { completed: true, result: { title: "Example Domain" } });
  assert.equal(programs.length, 1);
});

test("read-only mode rejects active Playwright methods", async () => {
  const { broker, programs } = harness();

  await assert.rejects(
    () => broker.runProgram("await page.locator('button').click();", false, "Click a button"),
    /interaction=true/,
  );
  assert.equal(programs.length, 0);
});

test("browser programs reject empty, oversized, failed, and malformed runner results", async () => {
  const { broker, context } = harness();

  await assert.rejects(() => broker.runProgram(" ", false, "Empty"), /1 to 20,000 bytes/);
  await assert.rejects(
    () => broker.runProgram("x".repeat(20_001), false, "Oversized"),
    /1 to 20,000 bytes/,
  );

  context.browserSession!.exec = async () => ({
    ok: false, exitCode: 1, stdout: "", stderr: "page crashed", durationMs: 1,
  });
  await assert.rejects(
    () => broker.runProgram("return true;", false, "Failing runner"),
    /page crashed/,
  );

  context.browserSession!.exec = async () => ({
    ok: true, exitCode: 0, stdout: "unexpected output", stderr: "", durationMs: 1,
  });
  await assert.rejects(
    () => broker.runProgram("return true;", false, "Malformed runner"),
    /invalid result/,
  );
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
