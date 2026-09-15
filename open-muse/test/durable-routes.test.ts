import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import type { AddressInfo } from "node:net";
import { request as httpRequest } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import type { Agent } from "@earendil-works/pi-agent-core";
import { createApp } from "../server/index.js";
import { ConversationManager, type RuntimeDependencies } from "../server/manager.js";
import type { ModelAccessService } from "../server/model-access.js";
import { ConversationStateStore, serializeConversation } from "../server/state-store.js";
import type { ConversationContext } from "../server/types.js";

test("trace endpoints are session-bound and stale generations request a resync", async (t) => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const server = createApp(manager);
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  const unauthenticated = await fetch(`${origin}/api/conversations/${created.id}/traces`);
  assert.equal(unauthenticated.status, 401);

  const bootstrapResponse = await fetch(`${origin}/api/bootstrap`);
  const cookie = bootstrapResponse.headers.get("set-cookie")!.split(";")[0]!;
  const snapshotResponse = await fetch(`${origin}/api/conversations/${created.id}/traces`, { headers: { cookie } });
  const snapshot = await snapshotResponse.json() as { streamId: string; cursor: number; turns: unknown[]; limits: Record<string, number> };
  assert.equal(snapshotResponse.status, 200);
  assert.deepEqual(snapshot.turns, []);
  assert.ok(snapshot.limits.maxPayloadBytes > 0);

  const nonLoopbackStatus = await new Promise<number>((resolve, reject) => {
    const request = httpRequest({ hostname: "127.0.0.1", port: (server.address() as AddressInfo).port, path: `/api/conversations/${created.id}/traces`, headers: { cookie, host: "example.com" } }, (response) => {
      response.resume();
      response.on("end", () => resolve(response.statusCode ?? 0));
    });
    request.on("error", reject);
    request.end();
  });
  assert.equal(nonLoopbackStatus, 403);

  const missing = await fetch(`${origin}/api/conversations/not-this-conversation/traces`, { headers: { cookie } });
  assert.equal(missing.status, 404);

  const resync = await fetch(`${origin}/api/conversations/${created.id}/traces/events?after=old-stream%3A0`, { headers: { cookie } });
  const body = await resync.text();
  assert.match(body, /event: trace\.resync_required/);
  assert.match(body, new RegExp(`id: ${snapshot.streamId}:${snapshot.cursor}`));
});

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

test("model-access mutations require CSRF and reject unknown JSON fields", async (t) => {
  const manager = new ConversationManager("", "gpt-5-mini");
  let cancelled = false;
  let started = false;
  const modelAccess = {
    snapshot: async () => ({ providers: [], disconnectingProviderIds: [] }),
    cancelSessionAttempts: () => undefined,
    close: () => undefined,
    cancelAttempt: () => { cancelled = true; return { id: "attempt", providerId: "openai", state: "cancelled", createdAt: new Date().toISOString(), expiresAt: new Date().toISOString() }; },
    startAttempt: () => { started = true; throw new Error("should not run"); },
  } as unknown as ModelAccessService;
  const server = createApp(manager, undefined, modelAccess);
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const bootstrapResponse = await fetch(`${origin}/api/bootstrap`);
  const bootstrap = await bootstrapResponse.json() as { csrfToken: string };
  const cookie = bootstrapResponse.headers.get("set-cookie")!.split(";")[0]!;

  const missingCsrf = await fetch(`${origin}/api/auth-attempts/attempt`, { method: "DELETE", headers: { "content-type": "application/json", cookie, origin }, body: "{}" });
  assert.equal(missingCsrf.status, 403);
  assert.equal(cancelled, false);

  const invalidBody = await fetch(`${origin}/api/auth-attempts`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-smol-csrf": bootstrap.csrfToken, cookie, origin },
    body: JSON.stringify({ providerId: "openai", method: "api_key", unexpected: true }),
  });
  assert.equal(invalidBody.status, 400);
  assert.deepEqual(await invalidBody.json(), { error: "Request body is invalid. Check the fields and try again.", code: "invalid_request" });
  assert.equal(started, false);
});

test("model-access routes preserve session selection and expose the complete local lifecycle", async (t) => {
  const now = new Date().toISOString();
  const calls: string[] = [];
  const attempt = { id: "attempt-positive", providerId: "test-provider", state: "waiting_for_input" as const, createdAt: now, expiresAt: now, prompt: { id: "prompt-positive", type: "secret" as const, message: "Enter key" } };
  const modelAccess = {
    models: {},
    snapshot: async (selection?: { providerId: string; modelId: string }) => ({
      providers: [{ id: "test-provider", name: "Test Provider", configured: true, source: "api_key" as const, methods: [{ type: "api_key" as const, label: "Use a key", enabled: true }], models: [{ id: "model-a", name: "Model A", recommended: true }, { id: "model-b", name: "Model B", recommended: false }] }],
      selection,
      disconnectingProviderIds: [],
    }),
    validateSelection: async (selection: { providerId: string; modelId: string }) => { calls.push(`validate:${selection.modelId}`); return { id: selection.modelId }; },
    preflight: async (selection: { providerId: string; modelId: string }) => { calls.push(`preflight:${selection.modelId}`); return { id: selection.modelId }; },
    startAttempt: () => { calls.push("start"); return attempt; },
    getAttempt: () => attempt,
    submitPrompt: (_session: string, _attempt: string, promptId: string, value: string) => { calls.push(`prompt:${promptId}:${value}`); return { ...attempt, state: "succeeded" as const, prompt: undefined }; },
    cancelAttempt: () => { calls.push("cancel"); return { ...attempt, state: "cancelled" as const, prompt: undefined }; },
    subscribe: (_session: string, _attempt: string, listener: (event: { id: number; type: string; snapshot: unknown }) => void) => {
      listener({ id: 1, type: "auth.succeeded", snapshot: { ...attempt, state: "succeeded", prompt: undefined } });
      return () => undefined;
    },
    logout: async (providerId: string) => { calls.push(`logout:${providerId}`); },
    cancelSessionAttempts: () => undefined,
    close: () => undefined,
  } as unknown as ModelAccessService;
  const fakeAgent = { state: { messages: [] }, abort: () => undefined, waitForIdle: async () => undefined } as unknown as Agent;
  const runtime = { createAgentWithModel: () => fakeAgent } satisfies Partial<RuntimeDependencies>;
  const manager = new ConversationManager("", "legacy", false, undefined, undefined, runtime, modelAccess);
  const server = createApp(manager, undefined, modelAccess);
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const bootstrapResponse = await fetch(`${origin}/api/bootstrap`);
  const bootstrap = await bootstrapResponse.json() as { csrfToken: string; modelAccess: { selection?: unknown } };
  const headers = { "content-type": "application/json", "x-smol-csrf": bootstrap.csrfToken, cookie: bootstrapResponse.headers.get("set-cookie")!.split(";")[0]!, origin };

  const selection = { providerId: "test-provider", modelId: "model-a" };
  assert.equal((await fetch(`${origin}/api/model-access`, { headers: { cookie: headers.cookie } })).status, 200);
  assert.equal((await fetch(`${origin}/api/model-access/selection`, { method: "PUT", headers, body: JSON.stringify(selection) })).status, 200);
  const selectedAccess = await (await fetch(`${origin}/api/model-access`, { headers: { cookie: headers.cookie } })).json() as { selection: typeof selection };
  assert.deepEqual(selectedAccess.selection, selection);

  const createdResponse = await fetch(`${origin}/api/conversations`, { method: "POST", headers, body: "{}" });
  const created = await createdResponse.json() as { id: string; modelId: string };
  assert.equal(created.modelId, "model-a");
  assert.equal(createdResponse.status, 201);

  const started = await fetch(`${origin}/api/auth-attempts`, { method: "POST", headers, body: JSON.stringify({ providerId: "test-provider", method: "api_key" }) });
  assert.equal(started.status, 201);
  assert.equal((await fetch(`${origin}/api/auth-attempts/${attempt.id}`, { headers: { cookie: headers.cookie } })).status, 200);
  assert.equal((await fetch(`${origin}/api/auth-attempts/${attempt.id}/prompts/${attempt.prompt.id}`, { method: "POST", headers, body: JSON.stringify({ value: "secret" }) })).status, 200);
  const eventResponse = await fetch(`${origin}/api/auth-attempts/${attempt.id}/events`, { headers: { cookie: headers.cookie } });
  assert.match(await eventResponse.text(), /event: auth\.succeeded/);
  assert.equal((await fetch(`${origin}/api/auth-attempts/${attempt.id}`, { method: "DELETE", headers, body: "{}" })).status, 202);

  const switchedResponse = await fetch(`${origin}/api/conversations/${created.id}/model-access`, { method: "PUT", headers, body: JSON.stringify({ providerId: "test-provider", modelId: "model-b" }) });
  assert.equal(switchedResponse.status, 200);
  assert.equal(((await switchedResponse.json()) as { modelId: string }).modelId, "model-b");
  assert.equal((await fetch(`${origin}/api/model-access/providers/test-provider`, { method: "DELETE", headers, body: "{}" })).status, 200);
  assert.equal((await fetch(`${origin}/api/conversations/${created.id}/model-access/reconnect`, { method: "POST", headers, body: "{}" })).status, 200);

  assert.deepEqual(calls, ["validate:model-a", "validate:model-a", "start", "prompt:prompt-positive:secret", "cancel", "preflight:model-b", "logout:test-provider", "preflight:model-b"]);
});

test("conversation history routes list, create, and activate saved chats", async (t) => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const first = await manager.create();
  const context = (manager as unknown as { context: ConversationContext }).context;
  context.messages.push({ id: "history-title", role: "user", text: "Plan a weekend trip", createdAt: new Date().toISOString() });
  const server = createApp(manager);
  await new Promise<void>((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const bootstrapResponse = await fetch(`${origin}/api/bootstrap`);
  const bootstrap = await bootstrapResponse.json() as { csrfToken: string; conversationId: string };
  const cookie = bootstrapResponse.headers.get("set-cookie")!.split(";")[0]!;
  const headers = { "content-type": "application/json", "x-smol-csrf": bootstrap.csrfToken, cookie, origin };

  assert.equal(bootstrap.conversationId, first.id);
  const initialList = await (await fetch(`${origin}/api/conversations`, { headers: { cookie } })).json() as { activeConversationId: string; conversations: Array<{ id: string; title: string }> };
  assert.equal(initialList.activeConversationId, first.id);
  assert.equal(initialList.conversations[0]?.title, "Plan a weekend trip");

  const createdResponse = await fetch(`${origin}/api/conversations`, { method: "POST", headers, body: JSON.stringify({ providerId: "openai", modelId: "gpt-5-mini" }) });
  const second = await createdResponse.json() as { id: string };
  assert.equal(createdResponse.status, 201);
  assert.notEqual(second.id, first.id);

  const activatedResponse = await fetch(`${origin}/api/conversations/${first.id}/activate`, { method: "POST", headers, body: "{}" });
  assert.equal(activatedResponse.status, 200);
  assert.equal(((await activatedResponse.json()) as { id: string }).id, first.id);
  assert.equal((await fetch(`${origin}/api/conversations/missing/activate`, { method: "POST", headers, body: "{}" })).status, 404);
  assert.equal((await fetch(`${origin}/api/conversations`, { method: "POST", headers, body: JSON.stringify({ providerId: "openai", extra: true }) })).status, 400);
});
