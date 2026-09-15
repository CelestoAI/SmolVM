import { useEffect, useRef, useState } from "react";
import * as api from "./api";

const SUGGESTION = "Open https://example.com and tell me what the page says";

function operationDetails(operation?: api.BrowserOperation): string | undefined {
  if (!operation) return;
  if (operation.kind === "navigate") return operation.url;
  if (operation.kind === "keypress") return operation.key;
  if (operation.kind === "select") return operation.label ? `Option: ${operation.label}` : undefined;
  return operation.target ? `${operation.target.role}: ${operation.target.name}` : undefined;
}

export function App() {
  const [conversation, setConversation] = useState<api.Conversation>();
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [viewerPath, setViewerPath] = useState("");
  const [controlEpoch, setControlEpoch] = useState("");
  const [approvalPending, setApprovalPending] = useState(false);
  const [recoveryPending, setRecoveryPending] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  const approvalPendingRef = useRef(false);
  const recoveryPendingRef = useRef(false);

  const refresh = async (id = conversation?.id) => { if (id) setConversation(await api.getConversation(id)); };
  useEffect(() => {
    let cancelled = false;
    let source: EventSource | undefined;
    void (async () => {
      try {
        const { conversationId } = await api.bootstrap();
        const created = conversationId ? await api.getConversation(conversationId) : await api.createConversation();
        if (cancelled) return;
        setConversation(created);
        source = new EventSource(`/api/conversations/${created.id}/events`);
        source.onmessage = () => void refresh(created.id);
        source.addEventListener("message.completed", () => void refresh(created.id));
        for (const name of ["browser.starting", "browser.ready", "agent.started", "agent.completed", "agent.failed", "tool.failed", "approval.requested", "approval.resolved", "approval.invalidated", "operation.approved", "operation.dispatched", "operation.completed", "operation.outcome_unknown", "popup.quarantined", "popup.adopted", "tab.navigated", "tab.closed", "control.changed", "cart.updated", "conversation.stopped"]) source.addEventListener(name, () => void refresh(created.id));
      } catch (caught) { if (!cancelled) setError(caught instanceof Error ? caught.message : "Could not start OpenMuse."); }
    })();
    return () => { cancelled = true; source?.close(); };
  }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [conversation?.messages.length]);
  useEffect(() => {
    if (!conversation?.viewerReady || viewerPath) return;
    void api.viewerToken(conversation.id).then(({ viewerPath: path }) => setViewerPath(path)).catch((caught) => setError(String(caught)));
  }, [conversation?.viewerReady, conversation?.id, viewerPath]);
  useEffect(() => { if (conversation?.runState === "stopped") setViewerPath(""); }, [conversation?.runState]);

  const submit = async (value = text) => {
    if (!conversation || !value.trim()) return;
    if (conversation.controlOwner !== "agent") {
      setError("Select Return control before sending a message to OpenMuse.");
      return;
    }
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
    if (!conversation?.pendingApproval || approvalPendingRef.current) return;
    const { id, pendingApproval } = conversation;
    approvalPendingRef.current = true;
    setApprovalPending(true);
    setError("");
    try { setConversation(await api.resolveApproval(id, pendingApproval, approved)); }
    catch (caught) {
      setError(caught instanceof Error ? caught.message : "Approval failed.");
      await refresh(id).catch(() => undefined);
    } finally {
      approvalPendingRef.current = false;
      setApprovalPending(false);
    }
  };
  const continueConversation = async () => {
    if (!conversation || recoveryPendingRef.current) return;
    recoveryPendingRef.current = true;
    setRecoveryPending(true);
    setError("");
    try { setConversation(await api.continueConversation(conversation.id)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Could not continue the conversation."); }
    finally { recoveryPendingRef.current = false; setRecoveryPending(false); }
  };
  const startOver = async () => {
    if (!conversation || recoveryPendingRef.current) return;
    recoveryPendingRef.current = true;
    setRecoveryPending(true);
    setError("");
    try { await api.startOver(conversation.id); window.location.reload(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Could not start over."); }
    finally { recoveryPendingRef.current = false; setRecoveryPending(false); }
  };

  const busy = approvalPending || conversation?.runState === "model_turn" || conversation?.runState === "tool_action";
  const interrupted = conversation?.runState === "interrupted";
  const humanControl = conversation?.controlOwner === "human";
  const pausingControl = conversation?.controlOwner === "pause_requested";
  const status = conversation?.runState === "stopped" ? "Stopped" : interrupted ? "Interrupted" : humanControl ? "You have control" : pausingControl ? "Pausing agent control…" : busy ? "Agent working" : conversation?.runState === "waiting_for_approval" ? "Waiting for you" : "Ready";
  const activities = [...(conversation?.events ?? [])].reverse().filter((event) => !["message.completed", "conversation.created"].includes(event.type)).slice(0, 8);
  const quarantinedPopups = (conversation?.tabs ?? []).filter((tab) => tab.owner === "quarantined");
  const recoveryCopy = conversation?.recovery?.kind === "failed_before_execution"
    ? {
        eyebrow: "Action did not run",
        title: "Choose how to proceed",
        detail: `${conversation.recovery.summary ? `${conversation.recovery.summary} did not run. ` : "The approved website action did not run. "}Continue so OpenMuse can re-plan and request fresh approval, or start over.`,
      }
    : conversation?.recovery?.kind === "outcome_unknown"
      ? {
          eyebrow: "Action outcome unknown",
          title: "Check before doing it again",
          detail: `${conversation.recovery.summary ? `${conversation.recovery.summary} may have completed. ` : "The approved website action may have completed. "}Continue in a fresh computer to inspect the result without repeating it, or start over.`,
        }
      : {
          eyebrow: "Work interrupted",
          title: "Choose how to proceed",
          detail: "OpenMuse stopped while working. The previous website action may have completed. Continue in a fresh computer, or start over.",
        };

  return <main className="app-shell">
    <header className="topbar">
      <div className="brand"><span className="brandmark">M</span><span>OpenMuse</span><span className="preview">PREVIEW</span></div>
      <div className="top-actions"><span className={`status-dot ${busy ? "working" : ""}`}></span><span>{status}</span>{conversation && conversation.runState !== "stopped" && <button className="quiet danger" onClick={() => void api.stopConversation(conversation.id)}>Stop</button>}</div>
    </header>
    <section className="workspace">
      <section className="chat-pane">
        <div className="chat-scroll">
          {!conversation?.messages.length && <div className="welcome"><div className="eyebrow">A computer coworker in a disposable VM</div><h1>What should we<br/>get done?</h1><p>Ask naturally. It can operate public websites in its own browser, while you watch, approve interactions, or take control.</p><button className="suggestion" onClick={() => void submit(SUGGESTION)}><span>Try a public web task</span><strong>{SUGGESTION}</strong><b>→</b></button></div>}
          <div className="messages">{conversation?.messages.map((message) => <article key={message.id} className={`message ${message.role}`}><div className="avatar">{message.role === "user" ? "Y" : "M"}</div><div><div className="message-role">{message.role === "user" ? "You" : "OpenMuse"}</div><p>{message.text}</p></div></article>)}</div>
          {interrupted && <aside className="approval recovery"><div className="eyebrow">{recoveryCopy.eyebrow}</div><h3>{recoveryCopy.title}</h3><p>{recoveryCopy.detail}</p><div><button disabled={recoveryPending} onClick={() => void continueConversation()}>Continue</button><button className="secondary" disabled={recoveryPending} onClick={() => void startOver()}>Start over</button></div></aside>}
          {quarantinedPopups.map((tab) => <aside className="approval" key={tab.id}><div className="eyebrow">Popup quarantined</div><h3>Use this new tab?</h3><p>{tab.url}</p><div><button onClick={() => void api.adoptPopup(conversation!.id, tab.id).then(setConversation).catch((caught) => setError(caught instanceof Error ? caught.message : "Could not adopt the popup."))}>Adopt tab</button></div></aside>)}
          {conversation?.pendingApproval && <aside className="approval"><div className="eyebrow">Approval required</div><h3>Allow this website interaction?</h3><p>{conversation.pendingApproval.reason}</p>{conversation.pendingApproval.pageUrl && <p>Current page: {conversation.pendingApproval.pageUrl}</p>}{operationDetails(conversation.pendingApproval.operation) && <p>{operationDetails(conversation.pendingApproval.operation)}</p>}<div><button disabled={approvalPending} onClick={() => void resolve(true)}>{approvalPending ? "Running…" : "Approve once"}</button><button className="secondary" disabled={approvalPending} onClick={() => void resolve(false)}>Not now</button></div></aside>}
          {busy && <div className="thinking"><i></i><i></i><i></i> Working in the browser</div>}
          <div ref={endRef}></div>
        </div>
        <div className="composer-wrap">{error && <div className="error">{error}</div>}<div className="composer"><textarea value={text} onChange={(event) => setText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void submit(); } }} placeholder={interrupted ? "Choose Continue or Start over…" : pausingControl ? "Pausing agent control…" : humanControl ? "Return control to message OpenMuse…" : "Message OpenMuse…"} disabled={!conversation || conversation.runState === "stopped" || interrupted || conversation.controlOwner !== "agent"}/><button aria-label="Send" onClick={() => void submit()} disabled={!text.trim() || interrupted || conversation?.controlOwner !== "agent"}>↑</button></div><div className="hint">{interrupted ? "Nothing will run until you choose" : pausingControl ? "Waiting for the current browser action to finish" : humanControl ? "Return control to continue chatting" : "Enter to send · SmolVM is deleted when you stop"}</div></div>
      </section>
      <section className="computer-pane">
        <div className="computer-head"><div><div className="eyebrow">Isolated workspace</div><h2>Agent’s computer</h2></div><div className="computer-actions">{conversation?.runState !== "stopped" && (humanControl ? <button onClick={() => void returnControl()}>Return control</button> : pausingControl ? <button className="secondary" disabled>Pausing…</button> : <button className="secondary" onClick={() => void takeControl()} disabled={!conversation?.viewerReady}>Take control</button>)}</div></div>
        <div className="screen">
          {viewerPath ? <iframe title="Live SmolVM computer" src={viewerPath}/> : <div className="screen-empty"><div className="orbit"><span>S</span></div><h3>{conversation?.runState === "stopped" ? "Computer deleted" : conversation?.sessionLifecycle === "starting" ? "Booting the computer…" : "The computer is asleep"}</h3><p>{conversation?.runState === "stopped" ? "Start a new conversation to get a fresh VM." : "It starts only when the agent needs a browser."}</p></div>}
          {viewerPath && conversation?.controlOwner === "agent" && <div className="input-shield"><span><i></i> LIVE · Agent controlling</span><button onClick={() => void takeControl()}>Take control</button></div>}
          {viewerPath && pausingControl && <div className="input-shield"><span><i></i> LIVE · Pausing agent control</span><button disabled>Pausing…</button></div>}
        </div>
        <div className="activity"><div className="activity-title"><span>Live activity</span><span>{activities.length ? "Current session" : "Waiting"}</span></div>{!activities.length ? <div className="activity-empty">Browser actions will appear here.</div> : activities.map((event) => <div className="activity-row" key={event.id}><span className="activity-icon"></span><div><strong>{String(event.payload.summary ?? event.type.replaceAll(".", " "))}</strong><small>{new Date(event.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</small></div></div>)}</div>
      </section>
    </section>
  </main>;
}
