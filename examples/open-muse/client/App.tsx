import { useEffect, useMemo, useState } from "react";
import { addConstraint, artifactUrl, cancelRun, createPlan, getRun, startRun, watchRun, type ArtifactName, type Run, type RunEvent } from "./api";
import { ArtifactPanel } from "./components/ArtifactPanel";
import { ComputerEvidence } from "./components/ComputerEvidence";
import { GoalPanel } from "./components/GoalPanel";
import { WorkTimeline } from "./components/WorkTimeline";

const DEFAULT_GOAL = "Plan a three-day Bengaluru trip for two under ₹40,000. Prioritize local food, architecture, and walkable neighborhoods. No nightlife.";
const DEFAULT_CONSTRAINTS = "Two travelers\nTotal budget at or below ₹40,000\nLocal food and architecture\nWalkable neighborhoods\nNo nightlife";
const terminal = new Set(["complete", "cancelled", "failed"]);

export function App() {
  const [goal, setGoal] = useState(DEFAULT_GOAL);
  const [constraintsText, setConstraintsText] = useState(DEFAULT_CONSTRAINTS);
  const [plan, setPlan] = useState<{ id: string; steps: string[] }>();
  const [run, setRun] = useState<Run>();
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [preview, setPreview] = useState<{ name: ArtifactName; content: string }>();
  const [finalSnapshotRetry, setFinalSnapshotRetry] = useState(0);
  const [finalSnapshotError, setFinalSnapshotError] = useState<string>();
  const [now, setNow] = useState(Date.now());
  const constraints = useMemo(() => constraintsText.split("\n").map((value) => value.trim()).filter(Boolean), [constraintsText]);
  const running = Boolean(run && !terminal.has(run.phase));

  useEffect(() => {
    if (!run || terminal.has(run.phase)) return;
    const stop = watchRun(run.id, (event) => {
      setEvents((current) => current.some((item) => item.id === event.id) ? current : [...current, event]);
      if (event.type === "run.phase" && event.phase) {
        setRun((current) => current ? { ...current, phase: event.phase! } : current);
      }
      if (event.type === "run.failed") setError([event.message, event.recovery].filter(Boolean).join(" "));
    }, () => {});
    return stop;
  }, [run?.id, running]);

  useEffect(() => {
    if (!run || !terminal.has(run.phase)) return;
    let cancelled = false;
    let retryTimer: number | undefined;
    const refreshFinalSnapshot = async () => {
      try {
        const latest = await getRun(run.id);
        if (!cancelled) {
          setRun(latest);
          setFinalSnapshotError(undefined);
        }
      } catch {
        if (cancelled) return;
        setFinalSnapshotError("The final artifact list could not be loaded. Retry now, or leave this page open for an automatic retry.");
        retryTimer = window.setTimeout(() => void refreshFinalSnapshot(), 2_000);
      }
    };
    void refreshFinalSnapshot();
    return () => { cancelled = true; if (retryTimer !== undefined) window.clearTimeout(retryTimer); };
  }, [run?.id, run?.phase, finalSnapshotRetry]);

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);

  const elapsed = run ? formatElapsed(now - Date.parse(run.startedAt)) : "00:00";
  const ready = useMemo(() => {
    return [...new Set(run?.artifacts.map((item) => item.name) ?? [])];
  }, [run?.artifacts]);

  async function prepare() {
    setBusy(true); setError(undefined); setFinalSnapshotError(undefined); setPreview(undefined);
    try {
      const result = await createPlan(goal, constraints);
      setPlan({ id: result.planId, steps: result.steps });
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }

  async function start() {
    if (!plan) return;
    setBusy(true); setError(undefined); setFinalSnapshotError(undefined); setEvents([]);
    try {
      const started = await startRun(plan.id);
      setPlan(undefined); setRun(started); setEvents(started.events); setNow(Date.now());
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(false); }
  }

  async function stop() {
    if (!run) return;
    try { await cancelRun(run.id); }
    catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); }
  }

  async function showPreview(name: ArtifactName) {
    if (!run) return;
    const response = await fetch(artifactUrl(run.id, name));
    if (!response.ok) return setError("That artifact is not ready yet.");
    setPreview({ name, content: await response.text() });
  }

  async function queueConstraint(value: string) {
    if (!run) return false;
    try {
      const latest = await addConstraint(run.id, value);
      setRun(latest);
      return true;
    } catch (cause) { setError(cause instanceof Error ? cause.message : String(cause)); return false; }
  }

  return <main className="shell">
    <header className="app-header"><div className="brand"><span className="brand-mark">M</span><strong>Open Muse</strong></div><div className="local-status"><span />{running ? `Running locally · ${run?.phase.replaceAll("_", " ")}` : "Local demo · ready"}</div></header>
    <GoalPanel goal={goal} constraints={constraintsText} plan={plan?.steps ?? (running ? run?.plan : undefined)} canStart={Boolean(plan)} busy={busy} running={running} onGoal={(value) => { setGoal(value); setPlan(undefined); }} onConstraints={(value) => { setConstraintsText(value); setPlan(undefined); }} onPlan={prepare} onStart={start} onStop={stop} onAddConstraint={queueConstraint} />
    <section className="workspace">
      <header className="workspace-header"><div><p className="eyebrow">Isolated workspace</p><h2>Muse's computer</h2></div><div className={`phase-pill ${running ? "active" : ""}`}><span />{run?.phase.replaceAll("_", " ") ?? "ready"}</div></header>
      {error && <div className="error-banner" role="alert"><strong>Open Muse needs attention</strong><span>{error}</span><button onClick={() => setError(undefined)}>Dismiss</button></div>}
      {finalSnapshotError && <div className="error-banner" role="alert"><strong>Open Muse needs attention</strong><span>{finalSnapshotError}</span><button onClick={() => setFinalSnapshotRetry((value) => value + 1)}>Retry</button></div>}
      <div className="work-grid">
        <ComputerEvidence events={events} />
        <WorkTimeline events={events} phase={run?.phase} elapsed={elapsed} />
      </div>
      <ArtifactPanel runId={run?.id} ready={ready} onPreview={showPreview} />
      {preview && <div className="preview" role="dialog" aria-modal="true" aria-labelledby="preview-title"><div className="preview-sheet"><header><h2 id="preview-title">{preview.name}</h2><button aria-label="Close preview" onClick={() => setPreview(undefined)}>×</button></header><pre>{preview.content}</pre></div></div>}
    </section>
  </main>;
}

function formatElapsed(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}
