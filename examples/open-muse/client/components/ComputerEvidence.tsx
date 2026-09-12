import type { RunEvent } from "../api";

export function ComputerEvidence({ events }: { events: RunEvent[] }) {
  const sources = events.filter((event) => event.type === "source.saved");
  const tools = events.filter((event) => event.type === "tool.started").slice(-5);
  return <section className="evidence-card" aria-label="Curated computer transcript">
    <header><span>/workspace/open-muse</span><span>network: public web only</span></header>
    <div className="terminal">
      {tools.length === 0 ? <p><span>$</span> waiting for an approved research step<span className="cursor" /></p> : tools.map((event) => <p key={event.id}><span>$</span> {event.tool?.replaceAll("_", " ")} <em>— {event.purpose}</em></p>)}
      {sources.slice(-2).map((event) => <p className="terminal-result" key={event.id}>saved source {event.source?.id}: {event.source?.title}</p>)}
      <p className="terminal-summary">{sources.length} source{sources.length === 1 ? "" : "s"} collected · host access disabled</p>
    </div>
  </section>;
}
