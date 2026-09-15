import { useEffect, useState } from "react";
import { activeElapsedMs, type TracePayload, type TraceStep, type TraceTurn } from "./trace";

function formatted(value: TracePayload | undefined): string {
  if (value === undefined) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function duration(ms: number): string {
  if (ms < 1_000) return "0s";
  if (ms < 60_000) return `${Math.round(ms / 1_000)}s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes}m ${Math.round((ms % 60_000) / 1_000)}s`;
}

export function TurnTrace({ turn }: { turn: TraceTurn }) {
  const [now, setNow] = useState(Date.now());
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (turn.state !== "running") return;
    const timer = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(timer);
  }, [turn.state]);
  const elapsed = duration(activeElapsedMs(turn, now));
  if (turn.state === "expired") return <div className="turn-trace-expired">Earlier run details expired</div>;
  const failedAt = turn.steps.findIndex((step) => step.state === "failed") + 1;
  const count = `${turn.steps.length} ${turn.steps.length === 1 ? "step" : "steps"}`;
  const title = turn.state === "running" ? `Working · ${count} · ${elapsed}`
    : turn.state === "waiting_for_approval" ? `Waiting for approval · ${count} · ${elapsed} active`
      : turn.state === "completed" ? `Worked for ${elapsed} · ${count}`
        : turn.state === "interrupted" ? `Interrupted after ${elapsed} · ${count}`
          : turn.state === "cancelled" ? `Stopped after ${elapsed} · ${count}` : `Stopped after ${elapsed}${failedAt ? ` · failed at step ${failedAt}` : ` · ${count}`}`;
  return <details className={`turn-trace ${turn.state}`} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary><span className="trace-caret" aria-hidden="true">›</span><span>{title}</span></summary>
    <div className="trace-body">
      <p className="trace-privacy">Run details may include page text and non-sensitive form values. Passwords, payment fields, and takeover-only input are never captured.</p>
      {!turn.steps.length && <p className="trace-empty">No tool details were recorded for this run.</p>}
      {turn.steps.map((step, index) => <TraceStepRow key={step.id} step={step} number={index + 1} autoOpen={open && step.state === "failed"}/>)}
    </div>
  </details>;
}

function TraceStepRow({ step, number, autoOpen }: { step: TraceStep; number: number; autoOpen: boolean }) {
  const [open, setOpen] = useState(false);
  useEffect(() => { if (autoOpen) setOpen(true); }, [autoOpen]);
  return <details className="trace-step" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary><span className={`trace-step-dot ${step.state}`}></span><span>{number}. {step.label}</span><small>{step.state}</small></summary>
    {open && <div className="trace-step-content">
      {step.input !== undefined && <section><h4>Input</h4><pre>{formatted(step.input)}</pre></section>}
      {step.output !== undefined && <section><h4>Result</h4><pre>{formatted(step.output)}</pre></section>}
      {step.error && <section><h4>Error</h4><pre>{step.error}</pre></section>}
      {step.input === undefined && step.output === undefined && !step.error && <p>No additional data.</p>}
    </div>}
  </details>;
}
