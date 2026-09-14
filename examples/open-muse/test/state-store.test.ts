import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";
import { ConversationManager } from "../server/manager.js";
import { ConversationStateStore, serializeConversation, type StoredConversation } from "../server/state-store.js";
import type { ConversationContext } from "../server/types.js";

async function temporaryStore(t: TestContext) {
  const directory = await mkdtemp(join(tmpdir(), "open-muse-state-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return new ConversationStateStore(join(directory, "state.json"));
}

async function conversationFixture() {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const context = (manager as unknown as { context: ConversationContext }).context;
  return { manager, created, context };
}

test("completed conversation messages survive manager reconstruction", async (t) => {
  const store = await temporaryStore(t);
  const { created, context } = await conversationFixture();
  context.messages.push(
    { id: "user-1", role: "user", text: "Find the total", createdAt: new Date().toISOString() },
    { id: "assistant-1", role: "assistant", text: "The total is ₹500.", createdAt: new Date().toISOString() },
  );
  await store.save(serializeConversation(context));

  const restored = await ConversationManager.open("", "gpt-5-mini", false, store);

  assert.equal(restored.activeConversationId, created.id);
  assert.deepEqual(restored.snapshot(created.id).messages, context.messages);
  assert.equal(restored.snapshot(created.id).runState, "idle");
  assert.equal(restored.snapshot(created.id).sessionLifecycle, "absent");
});

test("an accepted user message is durable before send returns", async (t) => {
  const store = await temporaryStore(t);
  const manager = new ConversationManager("", "gpt-5-mini", false, store);
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  internals.runTurn = async () => undefined;

  await manager.send(created.id, "Remember this request");
  const saved = await store.load();

  assert.equal(saved?.conversation.messages.at(-1)?.text, "Remember this request");
  assert.equal((await stat(store.path)).mode & 0o777, 0o600);
});

test("in-flight work restores as interrupted without replayable approval state", async (t) => {
  const store = await temporaryStore(t);
  const { created, context } = await conversationFixture();
  context.runState = "waiting_for_approval";
  context.sessionLifecycle = "ready";
  context.pendingApproval = {
    kind: "browser_program",
    approvalId: "approval-secret",
    actionDigest: "a".repeat(64),
    reason: "Place the order",
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    program: "await page.getByText('Buy').click()",
  };
  context.events.push({
    id: 2,
    conversationId: context.id,
    stateVersion: context.stateVersion,
    createdAt: new Date().toISOString(),
    type: "approval.requested",
    payload: { summary: "Place the order", approvalId: "approval-secret", program: context.pendingApproval.program },
  });
  await store.save(serializeConversation(context));

  const restored = await ConversationManager.open("", "gpt-5-mini", false, store);
  const snapshot = restored.snapshot(created.id);

  assert.equal(snapshot.runState, "interrupted");
  assert.equal(snapshot.sessionLifecycle, "absent");
  assert.equal(snapshot.controlOwner, "agent");
  assert.equal(snapshot.pendingApproval, undefined);
  const contents = await readFile(store.path, "utf8");
  assert.match(contents, /Place the order/);
  assert.doesNotMatch(contents, /approval-secret|getByText|"actionDigest"/);
});

test("human browser control never survives without its computer", async (t) => {
  const store = await temporaryStore(t);
  const { created, context } = await conversationFixture();
  context.controlOwner = "human";
  context.sessionLifecycle = "ready";
  await store.save(serializeConversation(context));

  const restored = await ConversationManager.open("", "gpt-5-mini", false, store);
  const snapshot = restored.snapshot(created.id);

  assert.equal(snapshot.controlOwner, "agent");
  assert.equal(snapshot.runState, "interrupted");
  assert.equal(snapshot.sessionLifecycle, "absent");
});

test("invalid state reports a recovery command instead of silently resetting", async (t) => {
  const store = await temporaryStore(t);
  await writeFile(store.path, '{"fileVersion":99}', "utf8");

  await assert.rejects(
    ConversationManager.open("", "gpt-5-mini", false, store),
    (error: unknown) => {
      const message = (error as Error).message;
      return message.includes(`mv \"${store.path}\" \"${store.path}.bad\"`) && message.includes("npm run dev");
    },
  );
});

test("serialized writes cannot let an older checkpoint overwrite a newer one", async (t) => {
  const store = await temporaryStore(t);
  const { context } = await conversationFixture();
  const older = serializeConversation(context);
  const newer = structuredClone(older) as StoredConversation;
  newer.conversation.stateVersion += 1;
  newer.conversation.messages.push({ id: "new", role: "user", text: "newest", createdAt: new Date().toISOString() });

  await Promise.all([store.save(older), store.save(newer)]);

  assert.equal((await store.load())?.conversation.messages.at(-1)?.text, "newest");
});

test("restored work stays paused until Continue is selected", async (t) => {
  const store = await temporaryStore(t);
  const { created, context } = await conversationFixture();
  context.runState = "model_turn";
  context.messages.push({ id: "user-continue", role: "user", text: "Finish the research", createdAt: new Date().toISOString() });
  await store.save(serializeConversation(context));
  const restored = await ConversationManager.open("", "gpt-5-mini", false, store);
  const internals = restored as unknown as {
    context: ConversationContext;
    turnQueue: Promise<void>;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  const prompts: string[] = [];
  internals.runTurn = async (_context, prompt) => { prompts.push(prompt); };

  await Promise.resolve();
  assert.equal(prompts.length, 0);
  assert.equal(restored.snapshot(created.id).runState, "interrupted");

  await restored.continueInterrupted(created.id);
  await internals.turnQueue;

  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /Finish the research/);
  assert.match(prompts[0], /Do not assume the interrupted website action succeeded/);
});

test("the first turn after an idle restart receives the visible conversation", async (t) => {
  const store = await temporaryStore(t);
  const { created, context } = await conversationFixture();
  context.messages.push(
    { id: "user-before-restart", role: "user", text: "Research train options", createdAt: new Date().toISOString() },
    { id: "assistant-before-restart", role: "assistant", text: "I found two routes.", createdAt: new Date().toISOString() },
  );
  await store.save(serializeConversation(context));
  const restored = await ConversationManager.open("", "gpt-5-mini", false, store);
  const internals = restored as unknown as {
    turnQueue: Promise<void>;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  const prompts: string[] = [];
  internals.runTurn = async (_context, prompt) => { prompts.push(prompt); };

  await restored.send(created.id, "Which route is faster?");
  await internals.turnQueue;

  assert.equal(prompts.length, 1);
  assert.match(prompts[0], /Research train options/);
  assert.match(prompts[0], /I found two routes/);
  assert.match(prompts[0], /Which route is faster/);
  assert.doesNotMatch(prompts[0], /interrupted website action/);
});

test("checkpoint serialization bounds messages and strips event payload details", async () => {
  const { context } = await conversationFixture();
  const createdAt = new Date().toISOString();
  context.messages = Array.from({ length: 105 }, (_, index) => ({
    id: `message-${index}`,
    role: index % 2 === 0 ? "user" as const : "assistant" as const,
    text: `message ${index}`,
    createdAt,
  }));
  context.events = Array.from({ length: 505 }, (_, index) => ({
    id: index + 1,
    conversationId: context.id,
    stateVersion: context.stateVersion,
    createdAt,
    type: "browser.event",
    payload: { summary: "s".repeat(1_100), secret: `secret-${index}` },
  }));

  const saved = serializeConversation(context).conversation;

  assert.equal(saved.messages.length, 100);
  assert.equal(saved.messages[0]?.id, "message-5");
  assert.equal(saved.events.length, 500);
  assert.equal(saved.events[0]?.id, 6);
  assert.equal(saved.events[0]?.payload.summary?.length, 1_000);
  assert.equal("secret" in saved.events[0]!.payload, false);
});

test("checkpoint serialization enforces the UTF-8 message byte budget", async () => {
  const { context } = await conversationFixture();
  const createdAt = new Date().toISOString();
  context.messages = [
    { id: "old", role: "user", text: "x".repeat(32_001), createdAt },
    { id: "new", role: "assistant", text: "🙂".repeat(8_000), createdAt },
  ];

  const saved = serializeConversation(context).conversation.messages;

  assert.deepEqual(saved.map((message) => message.id), ["new"]);
  assert.ok(Buffer.byteLength(saved[0]!.text, "utf8") <= 64_000);
});

test("a missing checkpoint starts without restored state", async (t) => {
  const store = await temporaryStore(t);

  assert.equal(await store.load(), undefined);
  const manager = await ConversationManager.open("", "gpt-5-mini", false, store);
  assert.equal(manager.activeConversationId, undefined);
});

test("Continue is one-shot and interrupted conversations reject new messages", async (t) => {
  const store = await temporaryStore(t);
  const { created, context } = await conversationFixture();
  context.runState = "tool_action";
  await store.save(serializeConversation(context));
  const restored = await ConversationManager.open("", "gpt-5-mini", false, store);
  const internals = restored as unknown as {
    turnQueue: Promise<void>;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  internals.runTurn = async () => undefined;

  await assert.rejects(
    restored.send(created.id, "repeat it"),
    (error: unknown) => (error as { status?: number }).status === 409,
  );
  await restored.continueInterrupted(created.id);
  await assert.rejects(
    restored.continueInterrupted(created.id),
    (error: unknown) => (error as { status?: number }).status === 409,
  );
  await internals.turnQueue;
});

test("Start over replaces interrupted work with a clean durable conversation", async (t) => {
  const store = await temporaryStore(t);
  const { created, context } = await conversationFixture();
  context.runState = "model_turn";
  context.messages.push({ id: "old", role: "user", text: "old work", createdAt: new Date().toISOString() });
  await store.save(serializeConversation(context));
  const restored = await ConversationManager.open("", "gpt-5-mini", false, store);

  const replacement = await restored.startOver(created.id);

  assert.notEqual(replacement.id, created.id);
  assert.equal(replacement.runState, "idle");
  assert.deepEqual(replacement.messages, []);
  assert.equal((await store.load())?.conversation.id, replacement.id);
});

test("graceful close preserves idle work but interrupts leased browser state", async (t) => {
  const idleStore = await temporaryStore(t);
  const idleManager = new ConversationManager("", "gpt-5-mini", false, idleStore);
  const idle = await idleManager.create();
  await idleManager.close();
  assert.equal((await idleStore.load())?.conversation.runState, "idle");

  const activeStore = await temporaryStore(t);
  const activeManager = new ConversationManager("", "gpt-5-mini", false, activeStore);
  const active = await activeManager.create();
  const context = (activeManager as unknown as { context: ConversationContext }).context;
  context.controlOwner = "human";
  context.sessionLifecycle = "ready";
  context.pendingApproval = {
    kind: "browser_program",
    approvalId: "approval-close",
    actionDigest: "e".repeat(64),
    reason: "Use the page",
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    program: "return true;",
  };

  await activeManager.close();

  const snapshot = activeManager.snapshot(active.id);
  assert.equal(snapshot.runState, "interrupted");
  assert.equal(snapshot.controlOwner, "agent");
  assert.equal(snapshot.sessionLifecycle, "absent");
  assert.equal(snapshot.pendingApproval, undefined);
  assert.equal((await activeStore.load())?.conversation.runState, "interrupted");
  assert.equal(idleManager.snapshot(idle.id).runState, "idle");
});

test("graceful close drains active and queued turns before releasing the computer", async (t) => {
  const store = await temporaryStore(t);
  const manager = new ConversationManager("", "gpt-5-mini", false, store);
  const created = await manager.create();
  const calls: string[] = [];
  let finishAction!: () => void;
  const activeAction = new Promise<void>((resolve) => { finishAction = resolve; });
  const internals = manager as unknown as {
    context: ConversationContext;
    activeAction: Promise<void>;
    turnQueue: Promise<void>;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
    releaseComputer: (context: ConversationContext) => Promise<void>;
  };
  internals.context.runState = "model_turn";
  internals.context.agent = {
    state: { messages: [], errorMessage: undefined },
    prompt: async () => { calls.push("prompt"); },
    abort: () => { calls.push("abort"); },
  } as unknown as ConversationContext["agent"];
  internals.activeAction = activeAction;
  internals.turnQueue = activeAction.then(() => internals.runTurn(internals.context, "queued"));
  internals.releaseComputer = async () => { calls.push("release"); };

  const closing = manager.close();
  await Promise.resolve();

  assert.equal(manager.snapshot(created.id).runState, "stopping");
  assert.deepEqual(calls, ["abort"]);

  finishAction();
  await closing;

  assert.deepEqual(calls, ["abort", "release"]);
  assert.equal(manager.snapshot(created.id).runState, "interrupted");
  assert.equal((await store.load())?.conversation.runState, "interrupted");
});

test("takeover, resume, and stop transitions are checkpointed", async (t) => {
  const store = await temporaryStore(t);
  const manager = new ConversationManager("", "gpt-5-mini", false, store);
  const created = await manager.create();

  const takeover = await manager.takeover(created.id);
  assert.equal((await store.load())?.conversation.controlOwner, "human");

  await manager.resume(created.id, takeover.controlEpoch);
  assert.equal((await store.load())?.conversation.controlOwner, "agent");

  await manager.stop(created.id);
  const stopped = await store.load();
  assert.equal(stopped?.conversation.runState, "stopped");
  assert.equal(stopped?.conversation.sessionLifecycle, "deleted");
});

test("approved browser actions clear approval data before the next durable turn", async (t) => {
  const store = await temporaryStore(t);
  const manager = new ConversationManager("", "gpt-5-mini", false, store);
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    turnQueue: Promise<void>;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  internals.runTurn = async () => undefined;
  internals.context.sessionLifecycle = "ready";
  internals.context.computer = {
    status: "ready",
    computerId: "computer-test",
    sandboxId: "sandbox-test",
    template: "linux-desktop",
    capabilities: [],
    display: {
      viewerUrl: "http://127.0.0.1:6080/vnc.html",
      vncUrl: "vnc://127.0.0.1:5900",
    },
    browser: {
      status: "ready",
      cdpUrl: "http://127.0.0.1:9222",
      launch: async () => undefined,
    },
    files: {
      read: async () => "",
      write: async () => undefined,
      upload: async () => undefined,
      download: async () => undefined,
    },
    exec: async () => ({
      ok: true,
      exitCode: 0,
      stdout: 'SMOLVM_BROWSER_RESULT={"ok":true,"value":{"programResult":{"done":true},"page":{"title":"Done","url":"https://example.com"}}}',
      stderr: "",
      durationMs: 1,
    }),
    delete: async () => undefined,
  };
  internals.context.pendingApproval = {
    kind: "browser_program",
    approvalId: "approval-durable",
    actionDigest: "f".repeat(64),
    reason: "Finish the action",
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    program: "return { done: true };",
  };

  await manager.approve(created.id, "approval-durable", "f".repeat(64), true);
  await internals.turnQueue;
  const checkpoint = await readFile(store.path, "utf8");

  assert.equal((await store.load())?.conversation.runState, "model_turn");
  assert.doesNotMatch(checkpoint, /approval-durable|actionDigest|program/);
});

test("terminal checkpoints restore without being mislabeled as interrupted", async (t) => {
  for (const runState of ["stopped", "failed"] as const) {
    const store = await temporaryStore(t);
    const { created, context } = await conversationFixture();
    context.runState = runState;
    context.sessionLifecycle = runState === "stopped" ? "deleted" : "error";
    await store.save(serializeConversation(context));

    const restored = await ConversationManager.open("", "gpt-5-mini", false, store);

    assert.equal(restored.snapshot(created.id).runState, runState);
    assert.equal(restored.activeConversationId, undefined);
  }
});

test("agent completion and failure states are durable", async (t) => {
  const successStore = await temporaryStore(t);
  const successManager = new ConversationManager("", "gpt-5-mini", false, successStore);
  const success = await successManager.create();
  const successInternals = successManager as unknown as {
    context: ConversationContext;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  successInternals.context.runState = "model_turn";
  successInternals.context.agent = {
    state: { messages: [], errorMessage: undefined },
    prompt: async () => undefined,
  } as unknown as ConversationContext["agent"];

  await successInternals.runTurn(successInternals.context, "complete");

  assert.equal(successManager.snapshot(success.id).runState, "idle");
  assert.equal((await successStore.load())?.conversation.runState, "idle");

  const failureStore = await temporaryStore(t);
  const failureManager = new ConversationManager("", "gpt-5-mini", false, failureStore);
  const failure = await failureManager.create();
  const failureInternals = failureManager as unknown as {
    context: ConversationContext;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  failureInternals.context.runState = "model_turn";
  failureInternals.context.agent = {
    state: { messages: [], errorMessage: undefined },
    prompt: async () => { throw new Error("model failed"); },
  } as unknown as ConversationContext["agent"];

  await failureInternals.runTurn(failureInternals.context, "fail");

  assert.equal(failureManager.snapshot(failure.id).runState, "failed");
  assert.equal((await failureStore.load())?.conversation.runState, "failed");
});
