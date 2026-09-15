export type TracePayload = null | boolean | number | string | TracePayload[] | { [key: string]: TracePayload };
export type TraceTurnState = "running" | "waiting_for_approval" | "completed" | "failed" | "cancelled" | "interrupted" | "expired";
export interface TraceStep {
  id: number; turnId: string; revision: number; kind: "model" | "tool" | "approval" | "system";
  label: string; state: "running" | "completed" | "failed" | "cancelled"; startedAt: string; completedAt?: string;
  input?: TracePayload; output?: TracePayload; error?: string;
}
export interface TraceTurn {
  conversationId: string; turnId: string; userMessageId: string; revision: number; state: TraceTurnState;
  startedAt: string; completedAt?: string; activeStartedAt?: string; accumulatedActiveMs: number; steps: TraceStep[];
}
export interface TraceSnapshot { streamId: string; cursor: number; conversationId: string; turns: TraceTurn[]; limits: Record<string, number> }
export type TraceEvent = {
  streamId: string; eventId: number; conversationId: string; createdAt: string;
} & ({ type: "trace.turn_upsert"; turn: TraceTurn }
  | { type: "trace.step_upsert"; turnId: string; turnRevision: number; step: TraceStep }
  | { type: "trace.turn_evict"; turnId: string }
  | { type: "trace.resync_required" });

export function applyTraceEvent(snapshot: TraceSnapshot, event: TraceEvent): TraceSnapshot {
  if (event.type === "trace.resync_required" || event.streamId !== snapshot.streamId || event.eventId <= snapshot.cursor) return snapshot;
  const turns = snapshot.turns.map((turn) => ({ ...turn, steps: [...turn.steps] }));
  if (event.type === "trace.turn_evict") return {
    ...snapshot,
    cursor: event.eventId,
    turns: turns.map((turn) => turn.turnId === event.turnId ? { ...turn, revision: turn.revision + 1, state: "expired", activeStartedAt: undefined, steps: [] } : turn),
  };
  if (event.type === "trace.turn_upsert") {
    const index = turns.findIndex((turn) => turn.turnId === event.turn.turnId);
    if (index < 0) turns.push(event.turn);
    else if (event.turn.revision >= turns[index].revision) turns[index] = event.turn;
  } else {
    const turn = turns.find((candidate) => candidate.turnId === event.turnId);
    if (turn) {
      const index = turn.steps.findIndex((step) => step.id === event.step.id);
      if (index < 0) turn.steps.push(event.step);
      else if (event.step.revision >= turn.steps[index].revision) turn.steps[index] = event.step;
      turn.revision = Math.max(turn.revision, event.turnRevision);
    }
  }
  return { ...snapshot, cursor: event.eventId, turns };
}

export function activeElapsedMs(turn: TraceTurn, now = Date.now()): number {
  return turn.accumulatedActiveMs + (turn.state === "running" && turn.activeStartedAt ? Math.max(0, now - Date.parse(turn.activeStartedAt)) : 0);
}
