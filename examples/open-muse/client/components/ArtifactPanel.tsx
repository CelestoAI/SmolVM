import { artifactUrl, packetUrl, type ArtifactName } from "../api";

const descriptions: Record<ArtifactName, string> = {
  "brief.md": "Recommendation and assumptions",
  "itinerary.md": "Three-day neighborhood plan",
  "budget.csv": "Itemized costs in INR",
  "sources.json": "Source provenance and hashes",
};

export function ArtifactPanel({ runId, ready, onPreview }: { runId?: string; ready: ArtifactName[]; onPreview: (name: ArtifactName) => void }) {
  const names = Object.keys(descriptions) as ArtifactName[];
  return <section className="artifacts-card">
    <header><div><p className="eyebrow">Research packet</p><h2>Your files</h2></div>{runId && ready.length === 4 && <a className="download" href={packetUrl(runId)}>Download packet ↓</a>}</header>
    <div className="artifact-grid">{names.map((name) => {
      const isReady = ready.includes(name);
      return <button key={name} disabled={!isReady || !runId} onClick={() => onPreview(name)}>
        <span className="file-icon">{name.endsWith(".md") ? "MD" : name.endsWith(".csv") ? "CSV" : "JSON"}</span>
        <span><strong>{name}</strong><small>{isReady ? descriptions[name] : "Waiting for Muse"}</small></span>
        {isReady && <span className="check">✓</span>}
      </button>;
    })}</div>
  </section>;
}
