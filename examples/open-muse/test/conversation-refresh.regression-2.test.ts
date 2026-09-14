import assert from "node:assert/strict";
import test from "node:test";
import { ConversationManager } from "../server/manager.js";
import type { ConversationContext } from "../server/types.js";

// Regression: ISSUE-002 — refreshing always created a second active conversation
// Found by /qa on 2026-09-13
// Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-09-13.md
test("bootstrap callers can recover the active conversation", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  assert.equal(manager.activeConversationId, created.id);
  assert.equal(manager.snapshot(manager.activeConversationId!).id, created.id);
  await manager.stop(created.id);
  assert.equal(manager.activeConversationId, undefined);
});

test("conversation creation and human control use single-owner state transitions", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  await assert.rejects(
    manager.create(),
    (error: unknown) => (error as { status?: number }).status === 409,
  );

  const takeover = await manager.takeover(created.id);
  assert.equal(manager.snapshot(created.id).controlOwner, "human");
  assert.deepEqual(await manager.takeover(created.id), takeover);
  assert.throws(
    () => manager.resume(created.id, "wrong-control-epoch"),
    (error: unknown) => (error as { status?: number }).status === 409,
  );

  const resumed = manager.resume(created.id, takeover.controlEpoch);
  assert.equal(resumed.controlOwner, "agent");
  await manager.stop(created.id);
});

test("replacing a failed conversation releases its disposable resources", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const context = (manager as unknown as { context: ConversationContext }).context;
  const closed: string[] = [];
  context.runState = "failed";
  context.playwright = { close: async () => { closed.push("playwright"); } } as ConversationContext["playwright"];
  context.browserSession = { delete: async () => { closed.push("browser"); } } as ConversationContext["browserSession"];
  context.smolvm = { close: async () => { closed.push("smolvm"); } } as ConversationContext["smolvm"];

  const replacement = await manager.create();

  assert.notEqual(replacement.id, created.id);
  assert.deepEqual(closed, ["playwright", "browser", "smolvm"]);
});

test("takeover waits for an approved browser action to finish", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const context = (manager as unknown as { context: ConversationContext }).context;
  let finishAction!: () => void;
  const actionFinished = new Promise<void>((resolve) => { finishAction = resolve; });
  context.sessionLifecycle = "ready";
  context.browserSession = {
    status: "ready", sessionId: "browser-test", sandboxId: "sandbox-test", cdpUrl: "http://127.0.0.1:9222",
    exec: async () => {
      await actionFinished;
      return { ok: true, exitCode: 0, stdout: 'SMOLVM_BROWSER_RESULT={"ok":true,"value":{"programResult":{},"page":{"title":"","url":"about:blank","visibleText":""}}}', stderr: "", durationMs: 1 };
    },
    delete: async () => undefined,
  };
  context.pendingApproval = {
    kind: "browser_program", approvalId: "approval-test", actionDigest: "a".repeat(64),
    reason: "Read the page", expiresAt: new Date(Date.now() + 60_000).toISOString(), program: "return {};",
  };

  const approval = manager.approve(created.id, "approval-test", "a".repeat(64), true);
  const takeover = manager.takeover(created.id);
  let takeoverFinished = false;
  void takeover.then(() => { takeoverFinished = true; });
  await Promise.resolve();
  assert.equal(takeoverFinished, false);

  finishAction();
  await approval;
  const control = await takeover;
  assert.equal(manager.snapshot(created.id).controlOwner, "human");
  assert.ok(control.controlEpoch);
  await manager.stop(created.id);
});

test("approved browser results resume the agent without requesting another observation", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    turnQueue: Promise<void>;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  const prompts: string[] = [];
  internals.runTurn = async (_context, text) => { prompts.push(text); };
  internals.context.sessionLifecycle = "ready";
  internals.context.browserSession = {
    status: "ready", sessionId: "browser-test", sandboxId: "sandbox-test", cdpUrl: "http://127.0.0.1:9222",
    exec: async () => ({
      ok: true, exitCode: 0,
      stdout: 'SMOLVM_BROWSER_RESULT={"ok":true,"value":{"programResult":{"title":"Amazon.in","price":"₹59,900"},"page":{"title":"Amazon.in","url":"https://amazon.in","visibleText":"iPhone ₹59,900"}}}',
      stderr: "", durationMs: 1,
    }),
    delete: async () => undefined,
  };
  internals.context.pendingApproval = {
    kind: "browser_program", approvalId: "approval-result", actionDigest: "b".repeat(64),
    reason: "Read the current price", expiresAt: new Date(Date.now() + 60_000).toISOString(),
    program: "return { title: await page.title() };",
  };

  await manager.approve(created.id, "approval-result", "b".repeat(64), true);
  await internals.turnQueue;

  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /"price":"₹59,900"/);
  assert.match(prompts[0], /"visibleText":"iPhone ₹59,900"/);
  assert.match(prompts[0], /without calling browser_run again/);
  assert.doesNotMatch(prompts[0], /Re-observe/);
  assert.equal(manager.snapshot(created.id).pendingApproval, undefined);
  await manager.stop(created.id);
});

test("duplicate approval submissions share one browser action", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    turnQueue: Promise<void>;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  internals.runTurn = async () => undefined;
  let finishAction!: () => void;
  const actionFinished = new Promise<void>((resolve) => { finishAction = resolve; });
  let executions = 0;
  internals.context.sessionLifecycle = "ready";
  internals.context.browserSession = {
    status: "ready", sessionId: "browser-test", sandboxId: "sandbox-test", cdpUrl: "http://127.0.0.1:9222",
    exec: async () => {
      executions += 1;
      await actionFinished;
      return { ok: true, exitCode: 0, stdout: 'SMOLVM_BROWSER_RESULT={"ok":true,"value":{"programResult":{},"page":{"title":"","url":"about:blank","visibleText":""}}}', stderr: "", durationMs: 1 };
    },
    delete: async () => undefined,
  };
  internals.context.pendingApproval = {
    kind: "browser_program", approvalId: "approval-duplicate", actionDigest: "c".repeat(64),
    reason: "Read the page", expiresAt: new Date(Date.now() + 60_000).toISOString(), program: "return {};",
  };

  const first = manager.approve(created.id, "approval-duplicate", "c".repeat(64), true);
  const duplicate = manager.approve(created.id, "approval-duplicate", "c".repeat(64), true);
  finishAction();
  const [firstSnapshot, duplicateSnapshot] = await Promise.all([first, duplicate]);

  assert.equal(executions, 1);
  assert.deepEqual(duplicateSnapshot, firstSnapshot);
  await manager.stop(created.id);
});

test("failed approved browser actions return an actionable error and clear stale approval state", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const context = (manager as unknown as { context: ConversationContext }).context;
  context.sessionLifecycle = "ready";
  context.browserSession = {
    status: "ready", sessionId: "browser-test", sandboxId: "sandbox-test", cdpUrl: "http://127.0.0.1:9222",
    exec: async () => ({ ok: false, exitCode: 1, stdout: "", stderr: "locator timed out", durationMs: 30_000 }),
    delete: async () => undefined,
  };
  context.pendingApproval = {
    kind: "browser_program", approvalId: "approval-failed", actionDigest: "d".repeat(64),
    reason: "Read the page", expiresAt: new Date(Date.now() + 60_000).toISOString(), program: "return {};",
  };
  const originalError = console.error;
  const logs: unknown[][] = [];
  console.error = (...args: unknown[]) => { logs.push(args); };

  try {
    await assert.rejects(
      manager.approve(created.id, "approval-failed", "d".repeat(64), true),
      (error: unknown) => (error as { status?: number; message?: string }).status === 422
        && (error as Error).message.includes("retry using the current page"),
    );
  } finally {
    console.error = originalError;
  }

  const snapshot = manager.snapshot(created.id);
  assert.equal(snapshot.pendingApproval, undefined);
  assert.equal(snapshot.runState, "idle");
  assert.equal(snapshot.events.at(-1)?.type, "tool.failed");
  assert.match(String(snapshot.events.at(-1)?.payload.summary), /retry using the current page/);
  assert.match(String(logs[0]?.[0]), /locator timed out/);
  await manager.stop(created.id);
});
