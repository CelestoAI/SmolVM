import { randomUUID } from "node:crypto";

export type TraceTurnState = "running" | "waiting_for_approval" | "completed" | "failed" | "cancelled" | "interrupted" | "expired";
export type TraceStepState = "running" | "completed" | "failed" | "cancelled";
export type TraceStepKind = "model" | "tool" | "approval" | "system";
export type TracePayload = null | boolean | number | string | TracePayload[] | { [key: string]: TracePayload };

export interface TurnExecution {
  conversationId: string;
  turnId: string;
  userMessageId: string;
}

export interface TraceStep {
  id: number;
  turnId: string;
  revision: number;
  kind: TraceStepKind;
  label: string;
  state: TraceStepState;
  startedAt: string;
  completedAt?: string;
  input?: TracePayload;
  output?: TracePayload;
  error?: string;
}

export interface TraceTurn extends TurnExecution {
  revision: number;
  state: TraceTurnState;
  startedAt: string;
  completedAt?: string;
  activeStartedAt?: string;
  accumulatedActiveMs: number;
  steps: TraceStep[];
}

export interface TraceSnapshot {
  streamId: string;
  cursor: number;
  conversationId: string;
  turns: TraceTurn[];
  limits: TraceLimits;
}

type TraceEventBody =
  | { type: "trace.turn_upsert"; turn: TraceTurn }
  | { type: "trace.step_upsert"; turnId: string; turnRevision: number; step: TraceStep }
  | { type: "trace.turn_evict"; turnId: string }
  | { type: "trace.resync_required" };

export type TraceEvent = {
  streamId: string;
  eventId: number;
  conversationId: string;
  createdAt: string;
} & TraceEventBody;

export interface TraceCursor { streamId: string; eventId: number }
export interface TraceSubscription { unsubscribe?: () => void; resync: boolean }
export interface TraceLimits {
  maxPayloadBytes: number;
  maxCanonicalBytes: number;
  maxJournalBytes: number;
  maxSteps: number;
  maxJournalEvents: number;
}

const DEFAULT_LIMITS: TraceLimits = {
  maxPayloadBytes: 64 * 1024,
  maxCanonicalBytes: 1_500 * 1024,
  maxJournalBytes: 512 * 1024,
  maxSteps: 500,
  maxJournalEvents: 1_000,
};

type Listener = (event: TraceEvent) => void;
type Normalizer = (value: unknown, maxBytes: number) => TracePayload;

function clone<T>(value: T): T {
  return structuredClone(value);
}

function byteLength(value: unknown): number {
  return Buffer.byteLength(JSON.stringify(value));
}

function safeText(value: unknown, max = 16_000): string {
  const text = typeof value === "string" ? value : String(value);
  return text.length <= max ? text : `${text.slice(0, max)}…`;
}

function normalizeValue(value: unknown, depth = 0, seen = new WeakSet<object>()): TracePayload {
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value : `[unsupported:${String(value)}]`;
  if (typeof value === "string") return safeText(value);
  if (typeof value !== "object") return `[unsupported:${typeof value}]`;
  if (depth >= 8) return "[depth limit]";
  if (seen.has(value)) return "[circular]";
  seen.add(value);
  if (Array.isArray(value)) {
    const output = value.slice(0, 200).map((item) => normalizeValue(item, depth + 1, seen));
    seen.delete(value);
    return output;
  }
  try {
    const output: Record<string, TracePayload> = {};
    for (const [key, item] of Object.entries(value).slice(0, 200)) output[safeText(key, 240)] = normalizeValue(item, depth + 1, seen);
    seen.delete(value);
    return output;
  } catch { return "[unreadable object]"; }
}

export function boundedTracePayload(value: unknown, maxBytes = DEFAULT_LIMITS.maxPayloadBytes): TracePayload {
  let payload = normalizeValue(value);
  if (byteLength(payload) <= maxBytes) return payload;
  const encoded = JSON.stringify(payload);
  payload = { truncated: true, preview: encoded.slice(0, Math.max(0, maxBytes - 80)) };
  while (byteLength(payload) > maxBytes && typeof payload === "object" && payload && "preview" in payload) {
    const previous = String(payload.preview);
    if (!previous) { delete payload.preview; break; }
    payload.preview = previous.slice(0, Math.max(0, previous.length - 512));
  }
  return byteLength(payload) <= maxBytes ? payload : null;
}

export function parseTraceCursor(value: string | undefined): TraceCursor | undefined {
  if (!value) return;
  const match = /^([^:]+):(\d+)$/.exec(value);
  if (!match) return;
  return { streamId: match[1], eventId: Number(match[2]) };
}

export class TraceBuffer {
  readonly streamId = randomUUID();
  private cursor = 0;
  private readonly turns = new Map<string, TraceTurn>();
  private readonly expiredTurns = new Map<string, TraceTurn>();
  private readonly journal: TraceEvent[] = [];
  private journalBytes = 0;
  private oldestEventId = 1;
  private canonicalBytes = 0;
  private stepCount = 0;
  private stepCursor = 0;
  private readonly listeners = new Set<Listener>();
  private readonly disabledTurns = new Set<string>();
  private readonly limits: TraceLimits;

  constructor(
    private readonly conversationId: string,
    limits: Partial<TraceLimits> = {},
    private readonly normalizer: Normalizer = boundedTracePayload,
  ) {
    this.limits = { ...DEFAULT_LIMITS, ...limits };
  }

  startTurn(execution: TurnExecution, startedAt = new Date().toISOString()): void {
    this.guard(execution.turnId, () => {
      if (execution.conversationId !== this.conversationId || this.turns.has(execution.turnId)) return;
      const turn: TraceTurn = { ...execution, revision: 1, state: "running", startedAt, activeStartedAt: startedAt, accumulatedActiveMs: 0, steps: [] };
      if (!this.reserveCanonical(turn)) return this.disable(execution, "Run details reached their memory limit.");
      this.turns.set(turn.turnId, turn);
      this.publish({ type: "trace.turn_upsert", turn: clone(turn) });
    });
  }

  setTurnState(execution: TurnExecution, state: TraceTurnState, at = new Date().toISOString()): void {
    this.guard(execution.turnId, () => {
      const turn = this.turns.get(execution.turnId);
      if (!turn || turn.state === state) return;
      const before = byteLength(turn);
      const now = Date.parse(at);
      if (turn.activeStartedAt) turn.accumulatedActiveMs += Math.max(0, now - Date.parse(turn.activeStartedAt));
      turn.activeStartedAt = state === "running" ? at : undefined;
      turn.state = state;
      turn.revision += 1;
      if (["completed", "failed", "cancelled", "interrupted"].includes(state)) turn.completedAt = at;
      else delete turn.completedAt;
      this.canonicalBytes += byteLength(turn) - before;
      this.publish({ type: "trace.turn_upsert", turn: clone(turn) });
    });
  }

  startStep(execution: TurnExecution, kind: TraceStepKind, label: string, input?: unknown): number | undefined {
    let id: number | undefined;
    this.guard(execution.turnId, () => {
      const turn = this.turns.get(execution.turnId);
      if (!turn || this.stepCount >= this.limits.maxSteps) return this.disable(execution, "Additional run details were omitted.");
      const step: TraceStep = {
        id: ++this.stepCursor, turnId: turn.turnId, revision: 1, kind, label: safeText(label, 240), state: "running", startedAt: new Date().toISOString(),
        ...(input === undefined ? {} : { input: this.normalizer(input, this.limits.maxPayloadBytes) }),
      };
      if (!this.reserveCanonical(step)) return this.disable(execution, "Additional run details were omitted.");
      turn.steps.push(step);
      turn.revision += 1;
      this.stepCount += 1;
      id = step.id;
      this.publish({ type: "trace.step_upsert", turnId: turn.turnId, turnRevision: turn.revision, step: clone(step) });
    });
    return id;
  }

  updateStepInput(execution: TurnExecution, stepId: number, input: unknown): void {
    this.updateStep(execution, stepId, (step) => { step.input = this.normalizer(input, this.limits.maxPayloadBytes); });
  }

  completeStep(execution: TurnExecution, stepId: number | undefined, output?: unknown): void {
    if (!stepId) return;
    this.updateStep(execution, stepId, (step) => {
      step.state = "completed";
      step.completedAt = new Date().toISOString();
      if (output !== undefined) step.output = this.normalizer(output, this.limits.maxPayloadBytes);
    });
  }

  failStep(execution: TurnExecution, stepId: number | undefined, error: unknown): void {
    if (!stepId) return;
    this.updateStep(execution, stepId, (step) => {
      step.state = "failed";
      step.completedAt = new Date().toISOString();
      step.error = safeText(error instanceof Error ? error.message : error, 2_000);
    });
  }

  cancelRunningSteps(execution: TurnExecution): void {
    this.guard(execution.turnId, () => {
      const turn = this.turns.get(execution.turnId);
      for (const step of turn?.steps ?? []) {
        if (step.state !== "running") continue;
        this.updateStep(execution, step.id, (current) => { current.state = "cancelled"; current.completedAt = new Date().toISOString(); });
      }
    });
  }

  snapshot(): TraceSnapshot {
    return { streamId: this.streamId, cursor: this.cursor, conversationId: this.conversationId, turns: [...this.expiredTurns.values(), ...this.turns.values()].map(clone), limits: { ...this.limits } };
  }

  subscribe(cursor: TraceCursor | undefined, listener: Listener): TraceSubscription {
    if (cursor && (cursor.streamId !== this.streamId || cursor.eventId > this.cursor || cursor.eventId < this.oldestEventId - 1)) {
      return { resync: true };
    }
    if (cursor) for (const event of this.journal) if (event.eventId > cursor.eventId) this.notify(listener, event);
    this.listeners.add(listener);
    return { resync: false, unsubscribe: () => this.listeners.delete(listener) };
  }

  close(): void {
    try { this.publish({ type: "trace.resync_required" }); } catch {}
    this.listeners.clear();
    this.turns.clear();
    this.expiredTurns.clear();
    this.journal.length = 0;
    this.canonicalBytes = 0;
    this.journalBytes = 0;
    this.stepCount = 0;
  }

  private updateStep(execution: TurnExecution, stepId: number, update: (step: TraceStep) => void): void {
    this.guard(execution.turnId, () => {
      const turn = this.turns.get(execution.turnId);
      const step = turn?.steps.find((candidate) => candidate.id === stepId);
      if (!turn || !step) return;
      const before = byteLength(step);
      update(step);
      step.revision += 1;
      turn.revision += 1;
      this.canonicalBytes += byteLength(step) - before;
      if (this.canonicalBytes > this.limits.maxCanonicalBytes) return this.disable(execution, "Additional run details were omitted.");
      this.publish({ type: "trace.step_upsert", turnId: turn.turnId, turnRevision: turn.revision, step: clone(step) });
    });
  }

  private reserveCanonical(value: unknown): boolean {
    const bytes = byteLength(value);
    this.evictCompleted(bytes);
    if (this.canonicalBytes + bytes > this.limits.maxCanonicalBytes) return false;
    this.canonicalBytes += bytes;
    return true;
  }

  private evictCompleted(requiredBytes: number): void {
    while (this.canonicalBytes + requiredBytes > this.limits.maxCanonicalBytes) {
      const oldest = [...this.turns.values()].find((turn) => ["completed", "failed", "cancelled", "interrupted"].includes(turn.state));
      if (!oldest) return;
      this.turns.delete(oldest.turnId);
      this.canonicalBytes = Math.max(0, this.canonicalBytes - byteLength(oldest));
      this.stepCount = Math.max(0, this.stepCount - oldest.steps.length);
      const tombstone: TraceTurn = { ...oldest, revision: oldest.revision + 1, state: "expired", activeStartedAt: undefined, steps: [] };
      this.expiredTurns.set(oldest.turnId, tombstone);
      while (this.expiredTurns.size > 100) this.expiredTurns.delete(this.expiredTurns.keys().next().value!);
      this.publish({ type: "trace.turn_evict", turnId: oldest.turnId });
    }
  }

  private disable(execution: TurnExecution, label: string): void {
    if (this.disabledTurns.has(execution.turnId)) return;
    this.disabledTurns.add(execution.turnId);
    const turn = this.turns.get(execution.turnId);
    if (!turn) return;
    const step: TraceStep = { id: ++this.stepCursor, turnId: turn.turnId, revision: 1, kind: "system", label, state: "completed", startedAt: new Date().toISOString(), completedAt: new Date().toISOString() };
    turn.steps.push(step);
    turn.revision += 1;
    this.publish({ type: "trace.step_upsert", turnId: turn.turnId, turnRevision: turn.revision, step: clone(step) });
  }

  private guard(turnId: string, operation: () => void): void {
    if (this.disabledTurns.has(turnId)) return;
    try { operation(); }
    catch { this.disable({ conversationId: this.conversationId, turnId, userMessageId: "" }, "Run details became unavailable."); }
  }

  private publish(event: TraceEventBody): void {
    const full = { ...event, streamId: this.streamId, eventId: ++this.cursor, conversationId: this.conversationId, createdAt: new Date().toISOString() } as TraceEvent;
    const bytes = byteLength(full);
    this.journal.push(full);
    this.journalBytes += bytes;
    while (this.journal.length > this.limits.maxJournalEvents || this.journalBytes > this.limits.maxJournalBytes) {
      const removed = this.journal.shift();
      if (!removed) break;
      this.journalBytes -= byteLength(removed);
      this.oldestEventId = removed.eventId + 1;
    }
    for (const listener of [...this.listeners]) this.notify(listener, full);
  }

  private notify(listener: Listener, event: TraceEvent): void {
    try { listener(clone(event)); }
    catch { this.listeners.delete(listener); }
  }
}

export interface ToolTraceAdapter {
  run<T>(tool: string, input: unknown, execute: () => Promise<T>): Promise<T>;
}
