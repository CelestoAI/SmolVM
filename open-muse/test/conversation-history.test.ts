import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";
import { ConversationManager } from "../server/manager.js";
import { ConversationStateStore } from "../server/state-store.js";
import type { ConversationContext } from "../server/types.js";

class ControlledStateStore extends ConversationStateStore {
  writes = 0;
  failAt?: number;

  override save(state: Parameters<ConversationStateStore["save"]>[0]): Promise<void> {
    this.writes += 1;
    if (this.writes === this.failAt) return Promise.reject(new Error("checkpoint failed"));
    return super.save(state);
  }
}

async function controlledStore(t: TestContext) {
  const directory = await mkdtemp(join(tmpdir(), "open-muse-history-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return new ControlledStateStore(join(directory, "state.json"));
}

function contextOf(manager: ConversationManager): ConversationContext {
  return (manager as unknown as { context: ConversationContext }).context;
}

test("new chats preserve titled history and saved chats can be resumed", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const first = await manager.create();
  contextOf(manager).messages.push({
    id: "first-message",
    role: "user",
    text: "  Research   the best train route from Delhi to Jaipur today  ",
    createdAt: new Date().toISOString(),
  });

  const second = await manager.create();
  const history = manager.list();

  assert.equal(history.activeConversationId, second.id);
  assert.equal(history.conversations.length, 2);
  assert.equal(history.conversations.find((item) => item.id === first.id)?.title, "Research the best train route from Delhi to Jaip");
  assert.equal(history.conversations.find((item) => item.id === second.id)?.title, "New chat");

  const resumed = await manager.activate(first.id);
  assert.equal(resumed.id, first.id);
  assert.equal(resumed.runState, "idle");
  assert.equal(resumed.messages[0]?.id, "first-message");
});

test("switching chats releases the outgoing disposable runtime", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  await manager.create();
  const calls: string[] = [];
  const context = contextOf(manager);
  context.playwright = { close: async () => { calls.push("playwright"); } } as ConversationContext["playwright"];
  context.computer = { delete: async () => { calls.push("computer"); } } as ConversationContext["computer"];
  context.smolvm = { close: async () => { calls.push("smolvm"); } } as ConversationContext["smolvm"];

  await manager.create();

  assert.deepEqual(calls, ["playwright", "computer", "smolvm"]);
  assert.equal(context.sessionLifecycle, "absent");
  assert.equal(context.agent, undefined);
});

test("conversation changes reject busy, human-controlled, and overlapping transitions", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const first = await manager.create();
  contextOf(manager).runState = "model_turn";
  await assert.rejects(manager.create(), (error: unknown) => (error as { code?: string }).code === "conversation_busy");

  contextOf(manager).runState = "idle";
  contextOf(manager).controlOwner = "human";
  await assert.rejects(manager.create(), (error: unknown) => (error as { code?: string }).code === "conversation_busy");

  contextOf(manager).controlOwner = "agent";
  let finishDelete!: () => void;
  const deleting = new Promise<void>((resolve) => { finishDelete = resolve; });
  contextOf(manager).computer = { delete: async () => deleting } as ConversationContext["computer"];
  const creating = manager.create();
  await Promise.resolve();
  await assert.rejects(manager.activate(first.id), (error: unknown) => (error as { code?: string }).code === "conversation_busy");
  finishDelete();
  await creating;
});

test("a turn cannot be accepted after conversation cleanup begins", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  await manager.create();
  let finishDelete!: () => void;
  const deleting = new Promise<void>((resolve) => { finishDelete = resolve; });
  const internals = manager as unknown as {
    context: ConversationContext;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  internals.runTurn = async () => undefined;
  internals.context.computer = { delete: async () => deleting } as ConversationContext["computer"];
  const creating = manager.create();
  await Promise.resolve();

  let code: string | undefined;
  try { await manager.send(internals.context.id, "Do not lose this turn"); }
  catch (error) { code = (error as { code?: string }).code; }
  finishDelete();
  await creating;

  assert.equal(code, "conversation_busy");
  assert.equal(manager.list().conversations.every((conversation) => conversation.title === "New chat"), true);
});

test("failed create and activate checkpoints preserve the active conversation", async (t) => {
  const store = await controlledStore(t);
  const manager = new ConversationManager("", "gpt-5-mini", false, store);
  const first = await manager.create();
  const listener = () => undefined;
  manager.subscribe(first.id, listener);
  const beforeCreate = manager as unknown as { context: ConversationContext; listeners: Set<unknown>; traces: unknown; replayConversationOnNextTurn: boolean };
  const originalContext = beforeCreate.context;
  const originalTraces = beforeCreate.traces;
  store.failAt = store.writes + 1;

  await assert.rejects(manager.create(), /checkpoint failed/);

  assert.equal(manager.activeConversationId, first.id);
  assert.equal(beforeCreate.context, originalContext);
  assert.equal(beforeCreate.traces, originalTraces);
  assert.equal(beforeCreate.listeners.size, 1);
  assert.equal(manager.list().conversations.length, 1);

  store.failAt = undefined;
  const second = await manager.create();
  const beforeActivate = (manager as unknown as { context: ConversationContext }).context;
  store.failAt = store.writes + 1;

  await assert.rejects(manager.activate(first.id), /checkpoint failed/);

  assert.equal(manager.activeConversationId, second.id);
  assert.equal((manager as unknown as { context: ConversationContext }).context, beforeActivate);
  assert.deepEqual(new Set(manager.list().conversations.map((conversation) => conversation.id)), new Set([first.id, second.id]));
});

test("a failed reset replacement keeps the stopped conversation active", async (t) => {
  const store = await controlledStore(t);
  const manager = new ConversationManager("", "gpt-5-mini", false, store);
  const first = await manager.create();
  const second = await manager.create();
  store.failAt = store.writes + 3;

  await assert.rejects(manager.startOver(second.id), /checkpoint failed/);

  assert.equal(manager.activeConversationId, second.id);
  assert.equal(manager.snapshot(second.id).runState, "stopped");
  assert.deepEqual(new Set(manager.list().conversations.map((conversation) => conversation.id)), new Set([first.id, second.id]));
  assert.equal((await store.load())?.activeConversationId, second.id);
});

test("reset removes the selected chat and activates the newest remaining chat", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const first = await manager.create();
  contextOf(manager).messages.push({ id: "keep-me", role: "user", text: "Saved work", createdAt: new Date().toISOString() });
  const second = await manager.create();
  contextOf(manager).messages.push({ id: "delete-me", role: "user", text: "Temporary work", createdAt: new Date().toISOString() });

  const replacement = await manager.startOver(second.id);
  const ids = manager.list().conversations.map((item) => item.id);

  assert.equal(replacement.id, first.id);
  assert.ok(ids.includes(first.id));
  assert.ok(!ids.includes(second.id));
  assert.equal(replacement.messages[0]?.text, "Saved work");
});

test("activating a stopped chat reopens its transcript without its old runtime", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const stopped = await manager.create();
  contextOf(manager).messages.push({ id: "saved", role: "user", text: "Reopen me", createdAt: new Date().toISOString() });
  await manager.stop(stopped.id);
  await manager.create();

  const reopened = await manager.activate(stopped.id);

  assert.equal(reopened.runState, "idle");
  assert.equal(reopened.sessionLifecycle, "absent");
  assert.equal(reopened.messages[0]?.text, "Reopen me");
});

test("multiple conversations and the active selection survive reconstruction", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "open-muse-history-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const store = new ConversationStateStore(join(directory, "state.json"));
  const manager = new ConversationManager("", "gpt-5-mini", false, store);
  const first = await manager.create();
  contextOf(manager).messages.push({ id: "saved", role: "user", text: "Keep this chat", createdAt: new Date().toISOString() });
  const second = await manager.create();

  const restored = await ConversationManager.open("", "gpt-5-mini", false, store);
  assert.equal(restored.activeConversationId, second.id);
  assert.deepEqual(new Set(restored.list().conversations.map((item) => item.id)), new Set([first.id, second.id]));
  assert.equal((await restored.activate(first.id)).messages[0]?.text, "Keep this chat");
});

test("history is capped without silently deleting an older chat", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  for (let index = 0; index < ConversationManager.maxConversations; index += 1) await manager.create();

  await assert.rejects(
    manager.create(),
    (error: unknown) => (error as { code?: string }).code === "conversation_limit",
  );
  assert.equal(manager.list().conversations.length, ConversationManager.maxConversations);
});
