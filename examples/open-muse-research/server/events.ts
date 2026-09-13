export type ArtifactName = "brief.md" | "itinerary.md" | "budget.csv" | "sources.json";

export type RunPhase =
  | "planning"
  | "awaiting_plan_approval"
  | "starting_sandbox"
  | "researching"
  | "building_packet"
  | "verifying"
  | "exporting"
  | "cleaning_up"
  | "complete"
  | "cancelled"
  | "failed";

export interface SourceRecord {
  id: string;
  url: string;
  finalUrl: string;
  title: string;
  retrievedAt: string;
  contentSha256: string;
}

export type RunEvent =
  | { id: number; type: "run.phase"; phase: RunPhase; at: string }
  | { id: number; type: "plan.ready"; steps: string[]; at: string }
  | { id: number; type: "vm.lifecycle"; name: string; progress?: number; at: string }
  | { id: number; type: "tool.started"; tool: string; purpose: string; at: string }
  | { id: number; type: "tool.completed"; tool: string; durationMs: number; summary: string; at: string }
  | { id: number; type: "source.saved"; source: Pick<SourceRecord, "id" | "url" | "title">; at: string }
  | { id: number; type: "artifact.ready"; name: ArtifactName; bytes: number; at: string }
  | { id: number; type: "run.warning"; message: string; recovery?: string; at: string }
  | { id: number; type: "run.failed"; message: string; recovery?: string; at: string }
  | { id: number; type: "run.completed"; durationMs: number; at: string };

export interface PublicRun {
  id: string;
  phase: RunPhase;
  goal: string;
  constraints: string[];
  plan: string[];
  startedAt: string;
  events: RunEvent[];
  artifacts: Array<{ name: ArtifactName; bytes: number }>;
  cleanupConfirmed: boolean;
}

export type RunEventInput = RunEvent extends infer Event
  ? Event extends RunEvent ? Omit<Event, "id" | "at"> : never
  : never;

export function publicEvent(event: RunEventInput, id: number): RunEvent {
  return { ...event, id, at: new Date().toISOString() } as RunEvent;
}
