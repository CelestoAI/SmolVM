import { useEffect, useRef, useState } from "react";
import * as api from "./api";

const SUGGESTION = "Add the best value wireless headphones under ₹8,000";

export function App() {
  const [conversation, setConversation] = useState<api.Conversation>();
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [viewerPath, setViewerPath] = useState("");
  const [controlEpoch, setControlEpoch] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  const refresh = async (id = conversation?.id) => { if (id) setConversation(await api.getConversation(id)); };
  useEffect(() => {
    let source: EventSource | undefined;
    void (async () => {
      try {
        await api.bootstrap();
        const created = await api.createConversation();
        setConversation(created);
        source = new EventSource(`/api/conversations/${created.id}/events`);
        source.onmessage = () => void refresh(created.id);
        source.addEventListener("message.completed", () => void refresh(created.id));
        for (const name of ["browser.starting", "browser.ready", "agent.started", "agent.completed", "agent.failed", "approval.requested", "approval.resolved", "control.changed", "cart.updated", "conversation.stopped"]) source.addEventListener(name, () => void refresh(created.id));
      } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not start Smol Agent."); }
    })();
    return () => source?.close();
  }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [conversation?.messages.length]);
  useEffect(() => {
    if (!conversation?.viewerReady || viewerPath) return;
    void api.viewerToken(conversation.id).then(({ viewerPath: path }) => setViewerPath(path)).catch((caught) => setError(String(caught)));
  }, [conversation?.viewerReady, conversation?.id, viewerPath]);

  const submit = async (value = text) => {
    if (!conversation || !value.trim()) return;
    setError(""); setText("");
    try { await api.sendMessage(conversation.id, value.trim()); await refresh(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Could not send message."); }
  };
  const takeControl = async () => {
    if (!conversation) return;
    try { const result = await api.takeOver(conversation.id); setControlEpoch(result.controlEpoch); await refresh(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Could not take control."); }
  };
  const returnControl = async () => {
    if (!conversation) return;
    try { setConversation(await api.resume(conversation.id, controlEpoch)); setControlEpoch(""); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Could not return control."); }
  };
  const resolve = async (approved: boolean) => {
    if (!conversation?.pendingApproval) return;
    try { setConversation(await api.resolveApproval(conversation.id, conversation.pendingApproval, approved)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Approval failed."); }
  };

  const busy = conversation?.runState === "model_turn" || conversation?.runState === "tool_action";
  const status = conversation?.runState === "stopped" ? "Stopped" : conversation?.controlOwner === "human" ? "You have control" : busy ? "Agent working" : conversation?.runState === "waiting_for_approval" ? "Waiting for you" : "Ready";
  const activities = [...(conversation?.events ?? [])].reverse().filter((event) => !["message.completed", "conversation.created"].includes(event.type)).slice(0, 8);

  return <main className="app-shell">
    <header className="topbar">
      <div className="brand"><span className="brandmark">S</span><span>Smol Agent</span><span className="preview">PREVIEW</span></div>
      <div className="top-actions"><span className={`status-dot ${busy ? "working" : ""}`}></span><span>{status}</span>{conversation && conversation.runState !== "stopped" && <button className="quiet danger" onClick={() => void api.stopConversation(conversation.id)}>Stop</button>}</div>
    </header>
    <section className="workspace">
      <section className="chat-pane">
        <div className="chat-scroll">
          {!conversation?.messages.length && <div className="welcome"><div className="eyebrow">A computer coworker in a disposable VM</div><h1>What should we<br/>get done?</h1><p>Ask naturally. Watch the browser work, step in when you want, and approve consequential actions at the boundary.</p><button className="suggestion" onClick={() => void submit(SUGGESTION)}><span>Try an offline shopping task</span><strong>{SUGGESTION}</strong><b>→</b></button></div>}
          <div className="messages">{conversation?.messages.map((message) => <article key={message.id} className={`message ${message.role}`}><div className="avatar">{message.role === "user" ? "Y" : "S"}</div><div><div className="message-role">{message.role === "user" ? "You" : "Smol Agent"}</div><p>{message.text}</p></div></article>)}</div>
          {conversation?.pendingApproval && <aside className="approval"><div className="eyebrow">Approval required</div><h3>Open checkout review?</h3><p>{conversation.pendingApproval.reason}</p><div><button onClick={() => void resolve(true)}>Approve once</button><button className="secondary" onClick={() => void resolve(false)}>Not now</button></div></aside>}
          {busy && <div className="thinking"><i></i><i></i><i></i> Working in the browser</div>}
          <div ref={endRef}></div>
        </div>
        <div className="composer-wrap">{error && <div className="error">{error}</div>}<div className="composer"><textarea value={text} onChange={(event) => setText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } }} placeholder="Message Smol Agent…" disabled={!conversation || conversation.runState === "stopped"}/><button aria-label="Send" onClick={() => void submit()} disabled={!text.trim()}>↑</button></div><div className="hint">Enter to send · SmolVM is deleted when you stop</div></div>
      </section>
      <section className="computer-pane">
        <div className="computer-head"><div><div className="eyebrow">Isolated workspace</div><h2>Agent’s computer</h2></div><div className="computer-actions">{conversation?.controlOwner === "human" ? <button onClick={() => void returnControl()}>Return control</button> : <button className="secondary" onClick={() => void takeControl()} disabled={!conversation?.viewerReady}>Take control</button>}</div></div>
        <div className="screen">
          {viewerPath ? <iframe title="Live SmolVM browser" src={viewerPath}/> : <div className="screen-empty"><div className="orbit"><span>S</span></div><h3>{conversation?.sessionLifecycle === "starting" ? "Booting the computer…" : "The computer is asleep"}</h3><p>It starts only when the agent needs a browser.</p></div>}
          {viewerPath && conversation?.controlOwner !== "human" && <div className="input-shield"><span><i></i> LIVE · Agent controlling</span><button onClick={() => void takeControl()}>Take control</button></div>}
        </div>
        <div className="activity"><div className="activity-title"><span>Live activity</span><span>{activities.length ? "Current session" : "Waiting"}</span></div>{!activities.length ? <div className="activity-empty">Browser actions will appear here.</div> : activities.map((event) => <div className="activity-row" key={event.id}><span className="activity-icon"></span><div><strong>{String(event.payload.summary ?? event.type.replaceAll(".", " "))}</strong><small>{new Date(event.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</small></div></div>)}</div>
      </section>
    </section>
  </main>;
}
