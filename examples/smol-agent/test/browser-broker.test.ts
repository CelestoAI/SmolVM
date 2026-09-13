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
