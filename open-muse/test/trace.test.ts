import assert from "node:assert/strict";
import test from "node:test";
import type { Agent } from "@earendil-works/pi-agent-core";
import { ConversationManager, type RuntimeDependencies } from "../server/manager.js";
import { boundedTracePayload, TraceBuffer, type ToolTraceAdapter, type TurnExecution } from "../server/trace.js";
import { applyTraceEvent, activeElapsedMs, type TraceSnapshot } from "../client/trace.js";

const execution: TurnExecution = { conversationId: "conversation", turnId: "turn", userMessageId: "message" };

test("trace buffer upserts one bounded step and pauses active time for approval", () => {
  const trace = new TraceBuffer(execution.conversationId);
  const events: unknown[] = [];
  trace.subscribe(undefined, (event) => events.push(event));
  trace.startTurn(execution, "2026-01-01T00:00:00.000Z");
  const stepId = trace.startStep(execution, "tool", "Observed the page", { text: "page text" });
  trace.completeStep(execution, stepId, { snapshot: "raw snapshot" });
  trace.setTurnState(execution, "waiting_for_approval", "2026-01-01T00:00:02.000Z");
  const snapshot = trace.snapshot();

  assert.equal(snapshot.turns[0].steps.length, 1);
  assert.equal(snapshot.turns[0].steps[0].revision, 2);
  assert.deepEqual(snapshot.turns[0].steps[0].output, { snapshot: "raw snapshot" });
  assert.equal(snapshot.turns[0].accumulatedActiveMs, 2_000);
  assert.equal(activeElapsedMs(snapshot.turns[0], Date.parse("2026-01-01T00:10:00.000Z")), 2_000);
  assert.equal(events.length, 4);
});

test("trace subscribers are isolated and invalid cursors require a snapshot", () => {
  const trace = new TraceBuffer(execution.conversationId);
  let delivered = 0;
  trace.subscribe(undefined, () => { throw new Error("subscriber failed"); });
  trace.subscribe(undefined, () => { delivered += 1; });
  trace.startTurn(execution);
  assert.equal(delivered, 1);
  assert.equal(trace.subscribe({ streamId: "older-generation", eventId: 0 }, () => undefined).resync, true);
  assert.equal(trace.subscribe({ streamId: trace.streamId, eventId: 99 }, () => undefined).resync, true);
});

test("trace recording failures add one sentinel without changing the caller", () => {
  const trace = new TraceBuffer(execution.conversationId, {}, () => { throw new Error("normalizer failed"); });
  trace.startTurn(execution);
  assert.doesNotThrow(() => trace.startStep(execution, "tool", "Broken detail", { value: true }));
  assert.deepEqual(trace.snapshot().turns[0].steps.map((step) => step.label), ["Run details became unavailable."]);
  assert.doesNotThrow(() => trace.startStep(execution, "tool", "Ignored", {}));
  assert.equal(trace.snapshot().turns[0].steps.length, 1);
});

test("trace payloads remain valid JSON within their byte cap", () => {
  const cyclic: { text: string; self?: unknown } = { text: "x".repeat(2_000) };
  cyclic.self = cyclic;
  const payload = boundedTracePayload(cyclic, 512);
  assert.ok(Buffer.byteLength(JSON.stringify(payload)) <= 512);
  assert.doesNotThrow(() => JSON.parse(JSON.stringify(payload)));
  assert.ok(Buffer.byteLength(JSON.stringify(boundedTracePayload(cyclic, 10))) <= 10);
});

test("client trace reducer ignores stale revisions and applies newer replacements", () => {
  const trace = new TraceBuffer(execution.conversationId);
  trace.startTurn(execution);
  const initial = trace.snapshot() as TraceSnapshot;
  const event = {
    type: "trace.turn_upsert" as const, streamId: initial.streamId, eventId: initial.cursor + 1,
    conversationId: execution.conversationId, createdAt: new Date().toISOString(),
    turn: { ...initial.turns[0], revision: initial.turns[0].revision + 1, state: "completed" as const },
  };
  const next = applyTraceEvent(initial, event);
  assert.equal(next.turns[0].state, "completed");
  assert.equal(applyTraceEvent(next, { ...event, eventId: event.eventId - 1 }).turns[0].state, "completed");
});

test("evicted runs remain visible as lightweight expiry rows", () => {
  const trace = new TraceBuffer(execution.conversationId, { maxCanonicalBytes: 450 });
  trace.startTurn(execution);
  trace.setTurnState(execution, "completed");
  const nextExecution = { ...execution, turnId: "turn-2", userMessageId: "message-2" };
  trace.startTurn(nextExecution);
  const snapshot = trace.snapshot();
  assert.equal(snapshot.turns.find((turn) => turn.turnId === execution.turnId)?.state, "expired");
  assert.deepEqual(snapshot.turns.find((turn) => turn.turnId === execution.turnId)?.steps, []);
});

test("manager associates traced tools and the assistant message with one product turn", async () => {
  let adapter: ToolTraceAdapter | undefined;
  const state = { messages: [] as Array<Record<string, unknown>>, errorMessage: undefined as string | undefined };
  const agent = {
    state,
    prompt: async () => {
      await adapter!.run("browser_observe", {}, async () => ({ snapshot: "agent-visible page" }));
      state.messages.push({ role: "assistant", content: [{ type: "text", text: "Done" }] });
    },
    abort: () => undefined,
    waitForIdle: async () => undefined,
  } as unknown as Agent;
  const runtime = {
    createAgent: ((_apiKey: string, _model: string, _broker: unknown, _fixture: boolean, trace?: ToolTraceAdapter) => { adapter = trace; return agent; }) as RuntimeDependencies["createAgent"],
  } satisfies Partial<RuntimeDependencies>;
  const manager = new ConversationManager("key", "gpt-5-mini", false, undefined, undefined, runtime);
  const conversation = await manager.create();
  await manager.send(conversation.id, "Inspect the page");
  await (manager as unknown as { turnQueue: Promise<void> }).turnQueue;

  const snapshot = manager.snapshot(conversation.id);
  const trace = manager.traceSnapshot(conversation.id);
  const user = snapshot.messages[0];
  const assistant = snapshot.messages[1];
  assert.equal(user.turnId, assistant.turnId);
  assert.equal(trace.turns[0].turnId, user.turnId);
  assert.equal(trace.turns[0].state, "completed");
  assert.deepEqual(trace.turns[0].steps[0].output, { snapshot: "agent-visible page" });
});
