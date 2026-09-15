import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { createApp } from "../server/index.js";
import { ConversationManager } from "../server/manager.js";
import { ConversationStateStore, serializeConversation } from "../server/state-store.js";
import type { ConversationContext } from "../server/types.js";

test("recovery routes continue interrupted work and start over with a clean conversation", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "open-muse-routes-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const store = new ConversationStateStore(join(directory, "state.json"));
  const initial = new ConversationManager("", "gpt-5-mini");
  const created = await initial.create();
  const initialContext = (initial as unknown as { context: ConversationContext }).context;
  initialContext.runState = "model_turn";
  initialContext.messages.push({
    id: "request",
    role: "user",
    text: "Finish this task",
    createdAt: new Date().toISOString(),
  });
  await store.save(serializeConversation(initialContext));

  const manager = await ConversationManager.open("", "gpt-5-mini", false, store);
  const internals = manager as unknown as {
    turnQueue: Promise<void>;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  internals.runTurn = async () => undefined;
  const server = createApp(manager);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  const bootstrapResponse = await fetch(`${origin}/api/bootstrap`);
  const bootstrap = await bootstrapResponse.json() as { csrfToken: string; conversationId?: string };
  const cookie = bootstrapResponse.headers.get("set-cookie")!.split(";")[0]!;
  const headers = {
    "content-type": "application/json",
    "x-smol-csrf": bootstrap.csrfToken,
    cookie,
    origin,
  };
  assert.equal(bootstrap.conversationId, created.id);

  const continuedResponse = await fetch(`${origin}/api/conversations/${created.id}/continue`, {
    method: "POST",
    headers,
    body: "{}",
  });
  const continued = await continuedResponse.json() as { id: string; runState: string };
  assert.equal(continuedResponse.status, 202);
  assert.equal(continued.id, created.id);
  assert.equal(continued.runState, "model_turn");
  await internals.turnQueue;

  const startOverResponse = await fetch(`${origin}/api/conversations/${created.id}/start-over`, {
    method: "POST",
    headers,
    body: "{}",
  });
  const replacement = await startOverResponse.json() as { id: string; runState: string; messages: unknown[] };
  assert.equal(startOverResponse.status, 201);
  assert.notEqual(replacement.id, created.id);
  assert.equal(replacement.runState, "idle");
  assert.deepEqual(replacement.messages, []);
});

test("message and resume routes wait for their checkpoints before responding", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "open-muse-awaited-routes-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const store = new ConversationStateStore(join(directory, "state.json"));
  const manager = new ConversationManager("", "gpt-5-mini", false, store);
  const created = await manager.create();
  const internals = manager as unknown as {
    turnQueue: Promise<void>;
    runTurn: (context: ConversationContext, text: string) => Promise<void>;
  };
  internals.runTurn = async () => undefined;
  const server = createApp(manager);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const bootstrapResponse = await fetch(`${origin}/api/bootstrap`);
  const bootstrap = await bootstrapResponse.json() as { csrfToken: string };
  const headers = {
    "content-type": "application/json",
    "x-smol-csrf": bootstrap.csrfToken,
    cookie: bootstrapResponse.headers.get("set-cookie")!.split(";")[0]!,
    origin,
  };

  const messageResponse = await fetch(`${origin}/api/conversations/${created.id}/messages`, {
    method: "POST",
    headers,
    body: JSON.stringify({ text: "Persist this before replying" }),
  });
  assert.equal(messageResponse.status, 202);
  assert.equal((await store.load())?.conversation.messages.at(-1)?.text, "Persist this before replying");
  await internals.turnQueue;

  const takeover = await manager.takeover(created.id);
  const resumeResponse = await fetch(`${origin}/api/conversations/${created.id}/resume`, {
    method: "POST",
    headers,
    body: JSON.stringify({ controlEpoch: takeover.controlEpoch }),
  });
  assert.equal(resumeResponse.status, 200);
  assert.equal((await store.load())?.conversation.controlOwner, "agent");
});
