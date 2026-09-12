import { randomBytes, randomUUID } from "node:crypto";
import { SmolVM } from "@celestoai/smolvm";
import { chromium } from "playwright-core";
import { ActionBroker } from "./broker.js";
import { assistantText, createAgent } from "./agent.js";
import { groundAddIntent } from "./intent.js";
import { installStorefront } from "./storefront.js";
import type { ConversationContext, ConversationEvent, Message } from "./types.js";

type Listener = (event: ConversationEvent) => void;

export class ConversationManager {
  private context?: ConversationContext;
  private listeners = new Set<Listener>();
  private turnQueue: Promise<void> = Promise.resolve();
  private viewerNonces = new Map<string, { conversationId: string; expiresAt: number }>();

  constructor(private readonly apiKey: string, private readonly model: string) {}

  create(): ReturnType<ConversationManager["snapshot"]> {
    if (this.context && !["stopped", "failed"].includes(this.context.runState)) throw Object.assign(new Error("Stop the active conversation before starting another."), { status: 409 });
    this.context = {
      id: randomUUID(), stateVersion: 1, controlOwner: "agent", runState: "idle", sessionLifecycle: "absent",
      messages: [], events: [], grants: [], cart: [], receipts: new Map(), commerceRevision: 0,
      observationId: "", lastActivityAt: Date.now(),
    };
    this.emit("conversation.created", { summary: "Conversation ready" });
    return this.snapshot(this.context.id);
  }

  snapshot(id: string) {
    const context = this.require(id);
    return {
      id: context.id, stateVersion: context.stateVersion, controlOwner: context.controlOwner,
      runState: context.runState, sessionLifecycle: context.sessionLifecycle,
      messages: context.messages, grants: context.grants.map(({ id: grantId, state, expiresAt }) => ({ id: grantId, state, expiresAt })),
      pendingApproval: context.pendingApproval, viewerReady: context.sessionLifecycle === "ready" && Boolean(context.browserSession?.viewerUrl),
      events: context.events,
    };
  }

  subscribe(id: string, listener: Listener, afterId = 0): (() => void) | undefined {
    const context = this.context;
    if (!context || context.id !== id) return;
    for (const event of context.events.filter((entry) => entry.id > afterId)) listener(event);
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  send(id: string, text: string): { accepted: true; stateVersion: number } {
    const context = this.require(id);
    if (context.runState === "stopped" || context.runState === "stopping") throw Object.assign(new Error("This conversation is stopped. Start a new one to continue."), { status: 409 });
    context.agent?.abort();
    for (const grant of context.grants) if (grant.state === "available" || grant.state === "reserved") grant.state = "cancelled";
    const message: Message = { id: randomUUID(), role: "user", text, createdAt: new Date().toISOString() };
    context.messages.push(message);
    const grant = groundAddIntent(message.id, text);
    if (grant) context.grants.push(grant);
    context.stateVersion += 1;
    context.lastActivityAt = Date.now();
    this.emit("message.completed", { message }, false);
    this.turnQueue = this.turnQueue.catch(() => undefined).then(() => this.runTurn(context, text));
    return { accepted: true, stateVersion: context.stateVersion };
  }

  async approve(id: string, approvalId: string, actionDigest: string, approved: boolean): Promise<ReturnType<ConversationManager["snapshot"]>> {
    const context = this.require(id);
    const broker = this.broker(context);
    await broker.resolveApproval(approvalId, actionDigest, approved);
    return this.snapshot(id);
  }

  async takeover(id: string): Promise<{ controlEpoch: string; stateVersion: number }> {
    const context = this.require(id);
    if (context.controlOwner === "human") return { controlEpoch: context.controlEpoch!, stateVersion: context.stateVersion };
    context.controlOwner = "pause_requested";
    context.stateVersion += 1;
    this.emit("control.changed", { owner: "pause_requested", summary: "Pausing agent control" }, false);
    context.agent?.abort();
    await context.agent?.waitForIdle();
    context.controlOwner = "human";
    context.controlEpoch = randomBytes(18).toString("base64url");
    context.runState = "idle";
    context.stateVersion += 1;
    this.emit("control.changed", { owner: "human", summary: "You have control" }, false);
    return { controlEpoch: context.controlEpoch, stateVersion: context.stateVersion };
  }

  resume(id: string, controlEpoch: string): ReturnType<ConversationManager["snapshot"]> {
    const context = this.require(id);
    if (context.controlOwner !== "human" || context.controlEpoch !== controlEpoch) throw Object.assign(new Error("Browser control changed. Take control again and retry."), { status: 409 });
    context.controlOwner = "agent";
    delete context.controlEpoch;
    context.stateVersion += 1;
    this.emit("control.changed", { owner: "agent", summary: "Agent control restored; it will re-observe before acting." }, false);
    return this.snapshot(id);
  }

  async stop(id: string): Promise<void> {
    const context = this.require(id);
    if (context.runState === "stopped") return;
    context.runState = "stopping";
    context.stateVersion += 1;
    this.emit("conversation.stopping", { summary: "Stopping the disposable computer" }, false);
    context.agent?.abort();
    await context.playwright?.close().catch(() => undefined);
    await context.browserSession?.delete().catch(() => undefined);
    await context.smolvm?.close().catch(() => undefined);
    context.runState = "stopped";
    context.sessionLifecycle = "deleted";
    context.stateVersion += 1;
    this.emit("conversation.stopped", { summary: "Disposable computer deleted" }, false);
  }

  issueViewerNonce(id: string): { viewerPath: string; expiresAt: string } {
    const context = this.require(id);
    if (!context.browserSession?.viewerUrl) throw Object.assign(new Error("The live browser is not ready yet."), { status: 409 });
    const nonce = randomBytes(32).toString("base64url");
    const expiresAt = Date.now() + 60_000;
    this.viewerNonces.set(nonce, { conversationId: id, expiresAt });
    const wsPath = `api/conversations/${id}/viewer/websockify?token=${nonce}`;
    return { viewerPath: `/api/conversations/${id}/viewer/vnc.html?autoconnect=1&resize=scale&path=${encodeURIComponent(wsPath)}`, expiresAt: new Date(expiresAt).toISOString() };
  }

  consumeViewerNonce(id: string, nonce: string): boolean {
    const value = this.viewerNonces.get(nonce);
    this.viewerNonces.delete(nonce);
    return Boolean(value && value.conversationId === id && value.expiresAt > Date.now());
  }

  viewerTarget(id: string): string {
    const url = this.require(id).browserSession?.viewerUrl;
    if (!url) throw Object.assign(new Error("The live browser is not ready."), { status: 409 });
    return new URL(url).origin;
  }

  async close(): Promise<void> { if (this.context) await this.stop(this.context.id); }

  private broker(context: ConversationContext): ActionBroker {
    return new ActionBroker(context, () => this.ensureBrowser(context), (type, payload, mutates = false) => {
      if (mutates) context.stateVersion += 1;
      this.emit(type, payload, false);
    });
  }

  private async runTurn(context: ConversationContext, text: string): Promise<void> {
    if (context.runState === "stopped" || context.controlOwner !== "agent") return;
    context.runState = "model_turn";
    context.stateVersion += 1;
    this.emit("agent.started", { summary: "Smol Agent is thinking" }, false);
    try {
      context.agent ??= createAgent(this.apiKey, this.model, this.broker(context));
      context.abortController = new AbortController();
      await context.agent.prompt(text);
      if (context.agent.state.errorMessage) throw new Error(context.agent.state.errorMessage);
      const textOutput = assistantText(context.agent).trim();
      if (textOutput) {
        const message: Message = { id: randomUUID(), role: "assistant", text: textOutput, createdAt: new Date().toISOString() };
        context.messages.push(message);
        this.emit("message.completed", { message }, false);
      }
      if (!context.pendingApproval) context.runState = "idle";
      context.stateVersion += 1;
      this.emit("agent.completed", { summary: context.pendingApproval ? "Waiting for approval" : "Ready" }, false);
    } catch (error) {
      if ((context.controlOwner as string) === "human" || (context.runState as string) === "stopping") return;
      context.runState = "failed";
      context.sessionLifecycle = context.sessionLifecycle === "ready" ? "ready" : "error";
      context.stateVersion += 1;
      const message = error instanceof Error ? error.message : "The agent turn failed.";
      this.emit("agent.failed", { summary: message }, false);
    }
  }

  private async ensureBrowser(context: ConversationContext): Promise<void> {
    if (context.sessionLifecycle === "ready") return;
    context.sessionLifecycle = "starting";
    context.runState = "tool_action";
    context.stateVersion += 1;
    this.emit("browser.starting", { summary: "Booting a disposable SmolVM browser" }, false);
    const smolvm = new SmolVM({ createTimeoutMs: 180_000 });
    context.smolvm = smolvm;
    try {
      const session = await smolvm.browsers.create({ mode: "live", profile: { mode: "ephemeral" }, viewport: { width: 1440, height: 900 }, network: { mode: "off" } });
      context.browserSession = session;
      const browser = await chromium.connectOverCDP(session.cdpUrl);
      context.playwright = browser;
      const browserContext = browser.contexts()[0] ?? await browser.newContext();
      browserContext.on("page", (newPage) => { if (context.page && newPage !== context.page) void newPage.close(); });
      const page = browserContext.pages()[0] ?? await browserContext.newPage();
      context.page = page;
      context.storefront = await installStorefront(browserContext, page, { cart: context.cart, receipts: context.receipts });
      context.sessionLifecycle = "ready";
      context.stateVersion += 1;
      this.emit("browser.ready", { summary: "Disposable browser ready", sandboxId: session.sandboxId }, false);
    } catch (error) {
      context.sessionLifecycle = "error";
      await smolvm.close().catch(() => undefined);
      throw error;
    }
  }

  private emit(type: string, payload: Record<string, unknown>, mutates = false): void {
    const context = this.context;
    if (!context) return;
    if (mutates) context.stateVersion += 1;
    const event: ConversationEvent = { id: (context.events.at(-1)?.id ?? 0) + 1, conversationId: context.id, stateVersion: context.stateVersion, createdAt: new Date().toISOString(), type, payload };
    context.events.push(event);
    if (context.events.length > 500) context.events.splice(0, context.events.length - 500);
    for (const listener of this.listeners) listener(event);
  }

  private require(id: string): ConversationContext {
    if (!this.context || this.context.id !== id) throw Object.assign(new Error("That conversation was not found."), { status: 404 });
    return this.context;
  }
}
