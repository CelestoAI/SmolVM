import { useEffect, useRef, useState } from "react";
import * as api from "./api";
import { MarkdownMessage } from "./MarkdownMessage";

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
  const [modelAccess, setModelAccess] = useState<api.ModelAccess>();
  const [authAttempt, setAuthAttempt] = useState<api.AuthAttempt>();
  const [authValue, setAuthValue] = useState("");
  const [selectedProviderId, setSelectedProviderId] = useState("");
  const [selectedModelId, setSelectedModelId] = useState("");
  const [showModelSetup, setShowModelSetup] = useState(false);
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
    void (async () => {
      try {
        const { conversationId, modelAccess: access } = await api.bootstrap();
        if (cancelled) return;
        setModelAccess(access);
        if (conversationId) setConversation(await api.getConversation(conversationId));
        else if (!access) setConversation(await api.createConversation());
        const pendingAttempt = sessionStorage.getItem("open_muse_auth_attempt");
        if (pendingAttempt) {
          try {
            const attempt = (await api.getAuthAttempt(pendingAttempt)).attempt;
            if (["succeeded", "expired", "cancelled", "failed"].includes(attempt.state)) sessionStorage.removeItem("open_muse_auth_attempt");
            else setAuthAttempt(attempt);
          }
          catch { sessionStorage.removeItem("open_muse_auth_attempt"); }
        }
      } catch (caught) { if (!cancelled) setError(caught instanceof Error ? caught.message : "Could not start OpenMuse."); }
    })();
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    if (!conversation?.id) return;
    const source = new EventSource(`/api/conversations/${conversation.id}/events`);
    const update = () => void refresh(conversation.id);
    source.onmessage = update;
    for (const name of ["message.completed", "browser.starting", "browser.ready", "agent.started", "agent.completed", "agent.failed", "model.auth_required", "tool.failed", "approval.requested", "approval.resolved", "approval.invalidated", "operation.approved", "operation.dispatched", "operation.completed", "operation.outcome_unknown", "popup.quarantined", "popup.adopted", "tab.navigated", "tab.closed", "control.changed", "cart.updated", "conversation.stopped"]) source.addEventListener(name, update);
    return () => source.close();
  }, [conversation?.id]);
  useEffect(() => {
    if (!authAttempt || ["succeeded", "expired", "cancelled", "failed"].includes(authAttempt.state)) return;
    const source = new EventSource(`/api/auth-attempts/${authAttempt.id}/events`);
    const update = (event: MessageEvent) => {
      const next = (JSON.parse(event.data) as { snapshot: api.AuthAttempt }).snapshot;
      setAuthAttempt(next);
      if (next.state === "succeeded") {
        sessionStorage.removeItem("open_muse_auth_attempt");
        void Promise.all([
          api.getModelAccess().then(setModelAccess),
          conversation?.modelAccessState === "auth_required" && conversation.providerId === next.providerId ? api.reconnectConversation(conversation.id).then(setConversation) : Promise.resolve(),
        ]).catch((caught) => setError(caught instanceof Error ? caught.message : "Could not finish connecting the model provider.")).finally(() => setAuthAttempt(undefined));
      }
    };
    for (const name of ["auth.started", "auth.prompt", "auth.prompt_submitted", "auth.prompt_cancelled", "auth.auth_url", "auth.device_code", "auth.progress", "auth.info", "auth.succeeded", "auth.failed", "auth.cancelled", "auth.expired", "auth.resync_required"]) source.addEventListener(name, update as EventListener);
    return () => source.close();
  }, [authAttempt?.id, authAttempt?.state, conversation?.id, conversation?.modelAccessState]);
  useEffect(() => {
    if (!modelAccess) return;
    const selected = modelAccess.selection;
    const provider = selected
      ? modelAccess.providers.find((candidate) => candidate.id === selected.providerId)
      : modelAccess.providers.find((candidate) => candidate.configured && candidate.models.length) ?? modelAccess.providers.find((candidate) => candidate.models.length);
    setSelectedProviderId(provider?.id ?? "");
    setSelectedModelId(selected?.modelId ?? provider?.models.find((model) => model.recommended)?.id ?? provider?.models[0]?.id ?? "");
  }, [modelAccess]);
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
  const beginAuth = async (providerId: string, method: "oauth" | "api_key") => {
    setError(""); setAuthValue("");
    try {
      const { attempt } = await api.startAuth(providerId, method);
      setAuthAttempt(attempt);
      sessionStorage.setItem("open_muse_auth_attempt", attempt.id);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not start sign-in."); }
  };
  const submitAuth = async () => {
    if (!authAttempt?.prompt || !authValue) return;
    try {
      const { attempt } = await api.submitAuthPrompt(authAttempt.id, authAttempt.prompt.id, authValue);
      setAuthValue(""); setAuthAttempt(attempt);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not continue sign-in."); }
  };
  const cancelAuth = async () => {
    if (!authAttempt) return;
    try { const { attempt } = await api.cancelAuth(authAttempt.id); setAuthAttempt(attempt); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Could not cancel sign-in."); }
    finally { sessionStorage.removeItem("open_muse_auth_attempt"); }
  };
  const startConversation = async (selection = { providerId: selectedProviderId, modelId: selectedModelId }) => {
    if (!selection.providerId || !selection.modelId) return;
    setError("");
    try {
      await api.selectModel(selection);
      setConversation(await api.createConversation());
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not start a conversation."); }
  };
  const disconnect = async (providerId: string) => {
    setError("");
    try {
      await api.disconnectProvider(providerId);
      setAuthAttempt(undefined);
      setModelAccess(await api.getModelAccess());
      if (conversation) setConversation(await api.getConversation(conversation.id));
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not disconnect the model provider."); }
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

  const needsModelSetup = Boolean(modelAccess && (showModelSetup || !conversation || conversation.modelAccessState !== "ready"));
  if (needsModelSetup) {
    const providers = modelAccess!.providers;
    const selectedProvider = providers.find((provider) => provider.id === selectedProviderId) ?? providers[0];
    const attemptProvider = modelAccess!.providers.find((provider) => provider.id === authAttempt?.providerId);
    const attemptActive = authAttempt && !["succeeded", "expired", "cancelled", "failed"].includes(authAttempt.state);
    return <main className="app-shell setup-shell">
      <header className="topbar"><div className="brand"><span className="brandmark">M</span><span>OpenMuse</span><span className="preview">PREVIEW</span></div><span className="setup-security">Credentials stay on this computer</span></header>
      <section className="setup-page">
        <div className="setup-intro"><div className="eyebrow">Model access</div><h1>{conversation?.modelAccessState === "ready" ? "Model settings" : conversation ? "Reconnect to continue" : "Choose how OpenMuse thinks"}</h1><p>{conversation?.modelAccessState === "ready" ? `This conversation uses ${conversation.modelId}. You can switch its model or disconnect a saved credential.` : conversation ? `This conversation uses ${conversation.modelId}. Connect its provider again to keep going.` : "Connect a supported provider, then choose the model for this conversation."}</p></div>
        {error && <div className="error setup-error">{error}</div>}
        {authAttempt ? <section className="auth-flow">
          <div className="eyebrow">{attemptProvider?.name ?? authAttempt.providerId}</div>
          <h2>{authAttempt.state === "succeeded" ? "Connected" : authAttempt.state === "failed" ? "Sign-in did not finish" : authAttempt.state === "expired" ? "Sign-in expired" : authAttempt.state === "cancelled" ? "Sign-in cancelled" : "Finish connecting"}</h2>
          {(authAttempt.error || authAttempt.message) && <p>{authAttempt.error ?? authAttempt.message}</p>}
          {authAttempt.authUrl && <><p>This opens a provider-controlled page in your browser, outside the agent's disposable computer.</p><button onClick={() => window.open(authAttempt.authUrl, "_blank", "noopener,noreferrer")}>Open secure sign-in page ↗</button></>}
          {authAttempt.deviceCode && <div className="device-code"><span>Enter this code</span><strong>{authAttempt.deviceCode.userCode}</strong><div><button onClick={() => void navigator.clipboard.writeText(authAttempt.deviceCode!.userCode).catch(() => setError("Could not copy the code. Select it and copy it manually."))}>Copy code</button><button className="secondary" onClick={() => window.open(authAttempt.deviceCode!.verificationUri, "_blank", "noopener,noreferrer")}>Open verification page ↗</button></div></div>}
          {authAttempt.prompt && <div className="auth-prompt"><label htmlFor="auth-response">{authAttempt.prompt.message}</label>{authAttempt.prompt.type === "select" ? <select id="auth-response" value={authValue} onChange={(event) => setAuthValue(event.target.value)}><option value="">Choose an option</option>{authAttempt.prompt.options?.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}</select> : <input id="auth-response" autoFocus type={["secret", "manual_code"].includes(authAttempt.prompt.type) ? "password" : "text"} autoComplete="off" placeholder={authAttempt.prompt.placeholder} value={authValue} onChange={(event) => setAuthValue(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void submitAuth(); }}/>}<button disabled={!authValue} onClick={() => void submitAuth()}>Continue</button></div>}
          {attemptActive ? <button className="text-button" onClick={() => void cancelAuth()}>Cancel</button> : <button className="secondary" onClick={() => { setAuthAttempt(undefined); setAuthValue(""); }}>Try again</button>}
        </section> : <section className="provider-grid">
          {providers.map((provider) => <article className={`provider-card ${provider.id === selectedProvider?.id ? "selected" : ""}`} key={provider.id}>
            <div className="provider-heading"><div><div className="eyebrow">Provider</div><h2>{provider.name}</h2></div>{provider.configured && <span className="connected">Connected · {provider.source === "account" ? "account" : provider.source === "environment" ? "environment" : "API key"}</span>}</div>
            {provider.configured ? <>
              <label className="model-label" htmlFor={`model-${provider.id}`}>Model</label><select id={`model-${provider.id}`} value={provider.id === selectedProviderId ? selectedModelId : provider.models[0]?.id ?? ""} onChange={(event) => { setSelectedProviderId(provider.id); setSelectedModelId(event.target.value); }}>{provider.models.map((model) => <option key={model.id} value={model.id}>{model.name}{model.recommended ? " · Recommended" : ""}</option>)}</select>
              <div className="provider-actions"><button onClick={() => { setSelectedProviderId(provider.id); const modelId = provider.id === selectedProviderId ? selectedModelId : provider.models[0]?.id ?? ""; setSelectedModelId(modelId); const sameBinding = conversation?.providerId === provider.id && conversation.modelId === modelId; if (conversation?.modelAccessState === "ready" && sameBinding) { setShowModelSetup(false); return; } void (!conversation ? startConversation({ providerId: provider.id, modelId }) : sameBinding ? api.reconnectConversation(conversation.id).then((next) => { setConversation(next); setShowModelSetup(false); }).catch((caught) => setError(caught instanceof Error ? caught.message : "Could not reconnect.")) : api.switchConversationModel(conversation.id, { providerId: provider.id, modelId }).then((next) => { setConversation(next); setShowModelSetup(false); }).catch((caught) => setError(caught instanceof Error ? caught.message : "Could not switch models."))); }}>{!conversation ? "Start using OpenMuse" : conversation.providerId === provider.id && conversation.modelId === (provider.id === selectedProviderId ? selectedModelId : provider.models[0]?.id) ? "Return to conversation" : conversation.providerId === provider.id ? "Switch model" : "Switch provider"}</button>{provider.source === "environment" ? <small>To disconnect, remove {provider.environmentVariable ?? "the provider credential"} from your environment and restart OpenMuse.</small> : <button className="text-button" onClick={() => void disconnect(provider.id)}>Disconnect</button>}</div>
            </> : <div className="auth-methods">{provider.methods.map((method) => method.type === "api_key" ? <details key={method.type}><summary>{method.label}</summary><p>The key is stored only on this computer and is never sent to the browser again.</p><button disabled={!method.enabled} onClick={() => void beginAuth(provider.id, method.type)}>Enter API key</button>{method.unavailableReason && <small>{method.unavailableReason}</small>}</details> : <div className="auth-method" key={method.type}><button disabled={!method.enabled} onClick={() => void beginAuth(provider.id, method.type)}>{method.label}</button>{method.unavailableReason && <small>{method.unavailableReason}</small>}</div>)}</div>}
          </article>)}
        </section>}
      </section>
    </main>;
  }

  return <main className="app-shell">
    <header className="topbar">
      <div className="brand"><span className="brandmark">M</span><span>OpenMuse</span><span className="preview">PREVIEW</span></div>
      <div className="top-actions">{modelAccess && conversation && <button className="quiet" onClick={() => setShowModelSetup(true)}>Model: {conversation.modelId}</button>}<span className={`status-dot ${busy ? "working" : ""}`}></span><span>{status}</span>{conversation && conversation.runState !== "stopped" && <button className="quiet danger" onClick={() => void api.stopConversation(conversation.id)}>Stop</button>}</div>
    </header>
    <section className="workspace">
      <section className="chat-pane">
        <div className="chat-scroll">
          {!conversation?.messages.length && <div className="welcome"><div className="eyebrow">A computer coworker in a disposable VM</div><h1>What should we<br/>get done?</h1><p>Ask naturally. It can operate public websites in its own browser, while you watch, approve interactions, or take control.</p><button className="suggestion" onClick={() => void submit(SUGGESTION)}><span>Try a public web task</span><strong>{SUGGESTION}</strong><b>→</b></button></div>}
          <div className="messages">{conversation?.messages.map((message) => <article key={message.id} className={`message ${message.role}`}><div className="avatar">{message.role === "user" ? "Y" : "M"}</div><div className="message-content"><div className="message-role">{message.role === "user" ? "You" : "OpenMuse"}</div>{message.role === "assistant" ? <MarkdownMessage>{message.text}</MarkdownMessage> : <p>{message.text}</p>}</div></article>)}</div>
          {interrupted && <aside className="approval recovery"><div className="eyebrow">{recoveryCopy.eyebrow}</div><h3>{recoveryCopy.title}</h3><p>{recoveryCopy.detail}</p><div><button disabled={recoveryPending} onClick={() => void continueConversation()}>Continue</button><button className="secondary" disabled={recoveryPending} onClick={() => void startOver()}>Start over</button></div></aside>}
          {quarantinedPopups.map((tab) => <aside className="approval" key={tab.id}><div className="eyebrow">Popup quarantined</div><h3>Use this new tab?</h3><p>{tab.url}</p><div><button disabled={interrupted} onClick={() => void api.adoptPopup(conversation!.id, tab.id).then(setConversation).catch((caught) => setError(caught instanceof Error ? caught.message : "Could not adopt the popup."))}>Adopt tab</button></div></aside>)}
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
