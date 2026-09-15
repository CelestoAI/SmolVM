import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { ConversationManager } from "../server/manager.js";
import { ConversationStateStore } from "../server/state-store.js";
import type { ConversationContext } from "../server/types.js";

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
