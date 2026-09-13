import { useRef, useState } from "react";

interface Props {
  goal: string;
  constraints: string;
  plan?: string[];
  canStart: boolean;
  busy: boolean;
  running: boolean;
  onGoal: (value: string) => void;
  onConstraints: (value: string) => void;
  onPlan: () => void;
  onStart: () => void;
  onStop: () => void;
  onAddConstraint: (value: string) => Promise<boolean>;
}

export function GoalPanel(props: Props) {
  const [nextConstraint, setNextConstraint] = useState("");
  const [addingConstraint, setAddingConstraint] = useState(false);
  const constraintPending = useRef(false);
  return <section className="goal-panel" aria-labelledby="goal-heading">
    <div className="goal-copy">
      <p className="eyebrow">Your goal</p>
      <h1 id="goal-heading">Turn an idea into a researched plan.</h1>
      <p className="lede">Muse works inside a temporary computer, keeps the useful files, then deletes the computer.</p>
    </div>
    <label>Goal<textarea rows={5} value={props.goal} disabled={props.running} onChange={(event) => props.onGoal(event.target.value)} /></label>
    <label>Constraints <span className="muted">one per line</span><textarea rows={6} value={props.constraints} disabled={props.running} onChange={(event) => props.onConstraints(event.target.value)} /></label>
    {props.plan && <div className="plan-card">
      <p className="eyebrow">Muse's plan</p>
      <ol>{props.plan.map((step) => <li key={step}>{step}</li>)}</ol>
    </div>}
    <div className="actions">
      {!props.canStart && !props.running && <button className="primary" disabled={props.busy} onClick={props.onPlan}>{props.busy ? "Preparing plan…" : "Prepare plan"}</button>}
      {props.canStart && !props.running && <button className="primary" disabled={props.busy} onClick={props.onStart}>Start research <span aria-hidden>→</span></button>}
      {props.running && <button className="danger" onClick={props.onStop}>Stop research</button>}
    </div>
    {props.running && <form className="constraint-row" onSubmit={async (event) => {
      event.preventDefault();
      const constraint = nextConstraint.trim();
      if (!constraint || constraintPending.current) return;
      constraintPending.current = true;
      setAddingConstraint(true);
      try {
        if (await props.onAddConstraint(constraint)) setNextConstraint("");
      } finally {
        constraintPending.current = false;
        setAddingConstraint(false);
      }
    }}>
      <label htmlFor="next-constraint">Add a constraint for the next phase</label>
      <div><input id="next-constraint" value={nextConstraint} maxLength={300} disabled={addingConstraint} onChange={(event) => setNextConstraint(event.target.value)} placeholder="Keep mornings unhurried" /><button type="submit" disabled={addingConstraint}>{addingConstraint ? "Sending…" : "Send"}</button></div>
    </form>}
    <p className="privacy"><span aria-hidden>◉</span> Your OpenAI key stays on the control server and never enters the VM.</p>
  </section>;
}
