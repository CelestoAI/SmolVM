import type { RunEvent, RunPhase } from "../api";

const labels: Partial<Record<RunPhase, string>> = {
  starting_sandbox: "Starting temporary computer",
  researching: "Researching the public web",
  building_packet: "Building your packet",
  verifying: "Checking citations and budget",
  exporting: "Saving the result",
  cleaning_up: "Deleting temporary computer",
  complete: "Packet ready",
  cancelled: "Research stopped",
  failed: "Run needs attention",
};

export function WorkTimeline({ events, phase, elapsed }: { events: RunEvent[]; phase?: RunPhase; elapsed: string }) {
  const visible = events.filter((event) => event.type !== "tool.completed" || event.summary?.includes("failed")).slice(-12).reverse();
  return <section className="timeline-card" aria-live="polite">
    <header><div><p className="eyebrow">Live activity</p><h2>{phase ? labels[phase] : "Ready when you are"}</h2></div><span className="timer">{elapsed}</span></header>
    {visible.length === 0 ? <div className="empty-state"><div className="orbit"><span /></div><p>The temporary computer's activity will appear here.</p></div> :
      <ol className="timeline">{visible.map((event) => <li key={event.id}>
        <span className={`event-dot ${event.type === "run.failed" ? "bad" : ""}`} />
        <div><strong>{eventLabel(event)}</strong><small>{new Date(event.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</small>
          {event.recovery && <code>{event.recovery}</code>}</div>
      </li>)}</ol>}
  </section>;
}

function eventLabel(event: RunEvent): string {
  if (event.type === "run.phase" && event.phase) return labels[event.phase] ?? event.phase;
  if (event.type === "vm.lifecycle") return event.name?.replaceAll(".", " ") ?? "Computer activity";
  if (event.type === "tool.started") return event.purpose ?? event.tool ?? "Research step";
  if (event.type === "source.saved") return `Saved ${event.source?.title ?? "a source"}`;
  if (event.type === "artifact.ready") return `${event.name} is ready`;
  return event.message ?? event.summary ?? "OpenMuse Research update";
}
