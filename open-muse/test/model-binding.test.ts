import assert from "node:assert/strict";
import test from "node:test";
import type { Agent } from "@earendil-works/pi-agent-core";
import type { ModelAccessService, ModelSelection } from "../server/model-access.js";
import { ConversationManager, type RuntimeDependencies } from "../server/manager.js";
import type { ConversationContext } from "../server/types.js";

function agent() {
  let aborted = false;
  let idleWaits = 0;
  const value = {
    state: { messages: [] },
    prompt: async () => undefined,
    abort: () => { aborted = true; },
    waitForIdle: async () => { idleWaits += 1; },
  } as unknown as Agent;
  return { value, aborted: () => aborted, idleWaits: () => idleWaits };
}

function fixture() {
  let configured = true;
  let logoutProvider: string | undefined;
  let preflightError: Error | undefined;
  let preflightWait: Promise<void> | undefined;
  const selections: ModelSelection[] = [];
  const replacement = agent();
  const service = {
    models: {},
    validateSelection: async (selection: ModelSelection) => {
      selections.push(selection);
      if (!configured) throw Object.assign(new Error("Connect Test Provider before choosing this model."), { status: 409, code: "provider_unconfigured" });
      return { id: selection.modelId };
    },
    preflight: async (selection: ModelSelection) => {
      await preflightWait;
      if (preflightError) throw preflightError;
      if (!configured) throw Object.assign(new Error("This model needs an account or API key before it can run."), { status: 409, code: "auth_required" });
      return { id: selection.modelId };
    },
    logout: async (providerId: string) => { logoutProvider = providerId; configured = false; },
    accessState: async () => configured ? "ready" as const : "auth_required" as const,
  } as unknown as ModelAccessService;
  const runtime = {
    createAgentWithModel: () => replacement.value,
  } satisfies Partial<RuntimeDependencies>;
  const manager = new ConversationManager("", "legacy", false, undefined, undefined, runtime, service);
  return {
    manager,
    replacement,
    selections,
    logoutProvider: () => logoutProvider,
    configure: () => { configured = true; },
    failPreflight: (error: Error) => { preflightError = error; },
    holdPreflight: (wait: Promise<void>) => { preflightWait = wait; },
  };
}

test("model-aware conversations require and retain an explicit selection", async () => {
  const { manager, selections } = fixture();

  await assert.rejects(manager.create(), /Choose a model/);
  const created = await manager.create({ providerId: "test-provider", modelId: "model-a" });

  assert.equal(created.providerId, "test-provider");
  assert.equal(created.modelId, "model-a");
  assert.equal(created.modelAccessState, "ready");
  assert.deepEqual(selections, [{ providerId: "test-provider", modelId: "model-a" }]);
});

test("switching models replaces an idle agent and preserves conversation history", async () => {
  const { manager, replacement } = fixture();
  const created = await manager.create({ providerId: "test-provider", modelId: "model-a" });
  const context = (manager as unknown as { context: ConversationContext }).context;
  const previous = agent();
  context.agent = previous.value;
  context.messages.push({ id: "message", role: "user", text: "Keep this", createdAt: new Date().toISOString() });

  const switched = await manager.switchModel(created.id, { providerId: "test-provider", modelId: "model-b" });

  assert.equal(switched.modelId, "model-b");
  assert.equal(switched.messages[0]?.text, "Keep this");
  assert.equal(previous.aborted(), true);
  assert.equal(previous.idleWaits(), 1);
  assert.equal(context.agent, replacement.value);
  context.runState = "model_turn";
  await assert.rejects(manager.switchModel(created.id, { providerId: "test-provider", modelId: "model-a" }), /current work to finish/);
});

test("disconnect and reconnect update the active conversation's access state", async () => {
  const { manager, logoutProvider, configure } = fixture();
  const created = await manager.create({ providerId: "test-provider", modelId: "model-a" });

  await manager.disconnectProvider("test-provider");
  assert.equal(logoutProvider(), "test-provider");
  assert.equal(manager.snapshot(created.id).modelAccessState, "auth_required");

  configure();
  const reconnected = await manager.reconnectProvider(created.id);
  assert.equal(reconnected.modelAccessState, "ready");
});

test("an expired credential pauses a turn without failing the conversation", async () => {
  const { manager, failPreflight } = fixture();
  const created = await manager.create({ providerId: "test-provider", modelId: "model-a" });
  failPreflight(Object.assign(new Error("Sign in again."), { status: 503, code: "auth_required" }));

  await manager.send(created.id, "Continue the task");
  await (manager as unknown as { turnQueue: Promise<void> }).turnQueue;

  const paused = manager.snapshot(created.id);
  assert.equal(paused.runState, "idle");
  assert.equal(paused.modelAccessState, "auth_required");
  assert.equal(paused.events.at(-1)?.type, "model.auth_required");
});

test("disconnect blocks new turns while the current agent settles", async () => {
  const { manager } = fixture();
  const created = await manager.create({ providerId: "test-provider", modelId: "model-a" });
  const context = (manager as unknown as { context: ConversationContext }).context;
  let release!: () => void;
  const settled = new Promise<void>((resolve) => { release = resolve; });
  context.agent = { state: { messages: [] }, abort: () => undefined, waitForIdle: () => settled } as unknown as Agent;

  const disconnecting = manager.disconnectProvider("test-provider");
  await assert.rejects(manager.send(created.id, "Do not start this turn"), /model change to finish/);
  release();
  await disconnecting;
});

test("model switching blocks new turns while credentials are checked", async () => {
  const { manager, holdPreflight } = fixture();
  const created = await manager.create({ providerId: "test-provider", modelId: "model-a" });
  let release!: () => void;
  const checked = new Promise<void>((resolve) => { release = resolve; });
  holdPreflight(checked);

  const switching = manager.switchModel(created.id, { providerId: "test-provider", modelId: "model-b" });
  await assert.rejects(manager.send(created.id, "Keep this on model A"), /model change to finish/);
  release();
  assert.equal((await switching).modelId, "model-b");
});

test("model switching blocks recovery actions while credentials are checked", async () => {
  const { manager, holdPreflight } = fixture();
  const created = await manager.create({ providerId: "test-provider", modelId: "model-a" });
  const context = (manager as unknown as { context: ConversationContext }).context;
  context.runState = "interrupted";
  let release!: () => void;
  const checked = new Promise<void>((resolve) => { release = resolve; });
  holdPreflight(checked);

  const switching = manager.switchModel(created.id, { providerId: "test-provider", modelId: "model-b" });
  await assert.rejects(manager.continueInterrupted(created.id), /model change to finish/);
  await assert.rejects(manager.startOver(created.id), /model change to finish/);
  release();
  assert.equal((await switching).modelId, "model-b");
});
