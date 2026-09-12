export type ArtifactName = "brief.md" | "itinerary.md" | "budget.csv" | "sources.json";
export type RunPhase = "starting_sandbox" | "researching" | "building_packet" | "verifying" | "exporting" | "cleaning_up" | "complete" | "cancelled" | "failed";

export interface RunEvent {
  id: number;
  type: string;
  at: string;
  phase?: RunPhase;
  name?: string;
  progress?: number;
  tool?: string;
  purpose?: string;
  durationMs?: number;
  summary?: string;
  source?: { id: string; url: string; title: string };
  bytes?: number;
  message?: string;
  recovery?: string;
}

export interface Run {
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

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  const body = await response.json() as T & { error?: string; recovery?: string };
  if (!response.ok) throw new Error([body.error, body.recovery].filter(Boolean).join(" "));
  return body;
}

export function createPlan(goal: string, constraints: string[]) {
  return json<{ planId: string; goal: string; constraints: string[]; steps: string[] }>("/api/plans", {
    method: "POST",
    body: JSON.stringify({ goal, constraints }),
  });
}

export function startRun(planId: string) {
  return json<Run>("/api/runs", { method: "POST", body: JSON.stringify({ planId }) });
}

export function getRun(id: string) { return json<Run>(`/api/runs/${id}`); }
export function cancelRun(id: string) { return json<Run>(`/api/runs/${id}/cancel`, { method: "POST", body: "{}" }); }
export function addConstraint(id: string, constraint: string) { return json<Run>(`/api/runs/${id}/constraints`, { method: "POST", body: JSON.stringify({ constraint }) }); }
export function artifactUrl(id: string, name: ArtifactName) { return `/api/runs/${id}/artifacts/${encodeURIComponent(name)}`; }
export function packetUrl(id: string) { return `/api/runs/${id}/packet.zip`; }

export function watchRun(id: string, onEvent: (event: RunEvent) => void, onDisconnect: () => void): () => void {
  const source = new EventSource(`/api/runs/${id}/events`);
  const names = ["run.phase", "vm.lifecycle", "tool.started", "tool.completed", "source.saved", "artifact.ready", "run.warning", "run.failed", "run.completed"];
  for (const name of names) source.addEventListener(name, (message) => onEvent(JSON.parse((message as MessageEvent).data)));
  source.onerror = onDisconnect;
  return () => source.close();
}
