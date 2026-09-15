import { randomBytes } from "node:crypto";
import {
  createModels,
  ModelsError,
  type Api,
  type AuthEvent,
  type AuthPrompt,
  type AuthType,
  type CredentialStore,
  type Model,
  type Models,
} from "@earendil-works/pi-ai";
import { openaiCodexProvider } from "@earendil-works/pi-ai/providers/openai-codex";
import { openaiProvider } from "@earendil-works/pi-ai/providers/openai";
import { FileCredentialStore } from "./credential-store.js";

const ATTEMPT_EVENT_LIMIT = 100;
const ATTEMPT_DEFAULT_MS = 5 * 60_000;
const ATTEMPT_MAX_MS = 20 * 60_000;
const TERMINAL_RETENTION_MS = 60_000;

export interface ModelSelection { providerId: string; modelId: string }
export interface ProviderAccessSummary {
  id: string;
  name: string;
  configured: boolean;
  source?: "account" | "api_key" | "environment";
  environmentVariable?: string;
  methods: Array<{ type: "oauth" | "api_key"; label: string; enabled: boolean; unavailableReason?: string }>;
  models: Array<{ id: string; name: string; recommended: boolean }>;
}
export interface ModelAccessSnapshot {
  providers: ProviderAccessSummary[];
  selection?: ModelSelection;
  disconnectingProviderIds: string[];
}

export type AuthAttemptState = "starting" | "waiting_for_browser" | "waiting_for_device" | "waiting_for_input" | "succeeded" | "expired" | "cancelled" | "failed";
export interface AuthPromptSnapshot {
  id: string;
  type: AuthPrompt["type"];
  message: string;
  placeholder?: string;
  options?: Array<{ id: string; label: string; description?: string }>;
}
export interface AuthAttemptSnapshot {
  id: string;
  providerId: string;
  state: AuthAttemptState;
  createdAt: string;
  expiresAt: string;
  prompt?: AuthPromptSnapshot;
  authUrl?: string;
  deviceCode?: { userCode: string; verificationUri: string };
  message?: string;
  error?: string;
}
export interface AuthAttemptEvent { id: number; type: string; snapshot: AuthAttemptSnapshot }

interface PendingPrompt {
  id: string;
  prompt: AuthPrompt;
  resolve(value: string): void;
  reject(error: Error): void;
}
interface AuthAttempt {
  id: string;
  ownerSessionId: string;
  providerId: string;
  state: AuthAttemptState;
  createdAt: number;
  expiresAt: number;
  controller: AbortController;
  prompt?: PendingPrompt;
  authUrl?: string;
  deviceCode?: { userCode: string; verificationUri: string };
  message?: string;
  error?: string;
  events: AuthAttemptEvent[];
  listeners: Set<(event: AuthAttemptEvent) => void>;
  terminalTimer?: ReturnType<typeof setTimeout>;
}

function appError(message: string, status: number, code: string): Error {
  return Object.assign(new Error(message), { status, code });
}

function safeText(value: string | undefined, maximum = 500): string | undefined {
  if (!value) return undefined;
  return value.replace(/https?:\/\/\S+/gi, "").replace(/\s+/g, " ").trim().slice(0, maximum) || undefined;
}

function safeHttpsUrl(value: string): string {
  const url = new URL(value);
  if (url.protocol !== "https:") throw new Error("Authentication link must use HTTPS.");
  if (value.length > 2_048) throw new Error("Authentication link is too long.");
  return url.toString();
}

export function sanitizeModelAccessError(error: unknown): Error {
  if (error && typeof error === "object" && typeof (error as { status?: unknown }).status === "number") return error as Error;
  if (error instanceof ModelsError && error.code === "oauth") return appError("This account needs you to sign in again.", 503, "auth_required");
  return appError("Account sign-in did not finish. Try again, or choose another sign-in method.", 503, "auth_failed");
}

export class ModelAccessService {
  readonly models: Models;
  private readonly attempts = new Map<string, AuthAttempt>();
  private readonly disconnecting = new Set<string>();

  constructor(models: Models, private readonly subscriptionAuthEnabled = false, private readonly credentialPath?: string) {
    this.models = models;
  }

  static createDefault(credentials: CredentialStore = new FileCredentialStore()): ModelAccessService {
    const models = createModels({ credentials });
    models.setProvider(openaiCodexProvider());
    models.setProvider(openaiProvider());
    return new ModelAccessService(models, process.env.OPEN_MUSE_ENABLE_SUBSCRIPTION_AUTH === "1", credentials instanceof FileCredentialStore ? credentials.path : undefined);
  }

  async snapshot(selection?: ModelSelection): Promise<ModelAccessSnapshot> {
    const providers = await Promise.all(this.models.getProviders().map(async (provider) => {
      const auth = await this.models.checkAuth(provider.id).catch(() => undefined);
      const configured = Boolean(auth);
      const displayName = provider.id === "openai-codex" ? "OpenAI account" : provider.id === "openai" ? "OpenAI API" : provider.name;
      const oauthEnabled = provider.auth.oauth?.isSubscription !== true || this.subscriptionAuthEnabled;
      const methods: ProviderAccessSummary["methods"] = [];
      if (provider.auth.oauth) methods.push({
        type: "oauth",
        label: provider.id === "openai-codex" ? "Continue with OpenAI" : provider.auth.oauth.loginLabel ?? `Continue with ${provider.name.replace(/\s*\(.+\)$/, "")}`,
        enabled: oauthEnabled,
        ...(!oauthEnabled ? { unavailableReason: "Account sign-in is available only in the development smoke until provider terms are approved." } : {}),
      });
      if (provider.auth.apiKey?.login) methods.push({ type: "api_key", label: provider.id === "openai" ? "Use an OpenAI API key" : `Use a ${provider.name} API key`, enabled: true });
      const source = !auth ? undefined : auth.type === "oauth" ? "account" as const : auth.source && !/stored/i.test(auth.source) ? "environment" as const : "api_key" as const;
      return {
        id: provider.id,
        name: displayName,
        configured,
        source,
        ...(source === "environment" && provider.id === "openai" ? { environmentVariable: "OPENAI_API_KEY" } : {}),
        methods,
        models: provider.getModels().map((model, index) => ({ id: model.id, name: model.name, recommended: index === 0 })),
      };
    }));
    return { providers, selection, disconnectingProviderIds: [...this.disconnecting] };
  }

  async validateSelection(selection: ModelSelection): Promise<Model<Api>> {
    if (selection.providerId.length > 80 || selection.modelId.length > 200) throw appError("That model selection is invalid.", 400, "invalid_selection");
    const model = this.models.getModel(selection.providerId, selection.modelId);
    if (!model) throw appError("That model is no longer available. Choose another model.", 409, "model_unavailable");
    const configured = await this.models.checkAuth(selection.providerId).catch(() => undefined);
    if (!configured) throw appError(`Connect ${this.models.getProvider(selection.providerId)?.name ?? "that provider"} before choosing this model.`, 409, "provider_unconfigured");
    return model;
  }

  async accessState(selection: ModelSelection): Promise<"ready" | "auth_required" | "model_unavailable"> {
    if (!this.models.getModel(selection.providerId, selection.modelId)) return "model_unavailable";
    return await this.models.checkAuth(selection.providerId).catch(() => undefined) ? "ready" : "auth_required";
  }

  async preflight(selection: ModelSelection): Promise<Model<Api>> {
    const model = await this.validateSelection(selection);
    try {
      const auth = await this.models.getAuth(model);
      if (!auth) throw appError("This model needs an account or API key before it can run.", 409, "auth_required");
      return model;
    } catch (error) { throw sanitizeModelAccessError(error); }
  }

  startAttempt(ownerSessionId: string, providerId: string, type: AuthType): AuthAttemptSnapshot {
    const provider = this.models.getProvider(providerId);
    if (!provider) throw appError("That model provider is not available.", 404, "provider_not_found");
    if (type === "oauth" && (!provider.auth.oauth || !this.subscriptionAuthEnabled)) throw appError("Account sign-in is not available for this provider.", 409, "method_unavailable");
    if (type === "api_key" && !provider.auth.apiKey?.login) throw appError("API-key setup is not available for this provider.", 409, "method_unavailable");
    if ([...this.attempts.values()].some((attempt) => attempt.providerId === providerId && !this.terminal(attempt.state))) {
      throw appError(`Sign-in is already open for ${provider.name}. Finish it, or cancel it before trying again.`, 409, "attempt_exists");
    }
    const now = Date.now();
    const attempt: AuthAttempt = {
      id: randomBytes(32).toString("base64url"), ownerSessionId, providerId, state: "starting", createdAt: now,
      expiresAt: now + ATTEMPT_DEFAULT_MS, controller: new AbortController(), events: [], listeners: new Set(),
    };
    this.attempts.set(attempt.id, attempt);
    this.emit(attempt, "auth.started");
    this.armExpiry(attempt);
    void this.runAttempt(attempt, type);
    return this.attemptSnapshot(attempt);
  }

  getAttempt(ownerSessionId: string, attemptId: string): AuthAttemptSnapshot {
    return this.attemptSnapshot(this.requireAttempt(ownerSessionId, attemptId));
  }

  submitPrompt(ownerSessionId: string, attemptId: string, promptId: string, value: string): AuthAttemptSnapshot {
    const attempt = this.requireAttempt(ownerSessionId, attemptId);
    const pending = attempt.prompt;
    if (!pending || pending.id !== promptId) throw appError("That sign-in question is no longer active. Refresh and try again.", 409, "stale_prompt");
    const maximum = pending.prompt.type === "secret" ? 16_384 : pending.prompt.type === "manual_code" ? 8_192 : 1_024;
    if (!value || value.length > maximum) throw appError("That sign-in response is invalid.", 400, "invalid_prompt_response");
    delete attempt.prompt;
    pending.resolve(value);
    this.emit(attempt, "auth.prompt_submitted");
    return this.attemptSnapshot(attempt);
  }

  cancelAttempt(ownerSessionId: string, attemptId: string): AuthAttemptSnapshot {
    const attempt = this.requireAttempt(ownerSessionId, attemptId);
    if (!this.terminal(attempt.state)) {
      attempt.state = "cancelled";
      attempt.controller.abort(new Error("Sign-in cancelled."));
      attempt.prompt?.reject(new Error("Sign-in cancelled."));
      delete attempt.prompt;
      this.emit(attempt, "auth.cancelled");
      this.retainTerminal(attempt);
    }
    return this.attemptSnapshot(attempt);
  }

  cancelSessionAttempts(ownerSessionId: string): void {
    for (const attempt of this.attempts.values()) if (attempt.ownerSessionId === ownerSessionId && !this.terminal(attempt.state)) this.cancelAttempt(ownerSessionId, attempt.id);
  }

  close(): void {
    for (const attempt of this.attempts.values()) {
      if (!this.terminal(attempt.state)) this.cancelAttempt(attempt.ownerSessionId, attempt.id);
      if (attempt.terminalTimer) clearTimeout(attempt.terminalTimer);
    }
    this.attempts.clear();
  }

  subscribe(ownerSessionId: string, attemptId: string, listener: (event: AuthAttemptEvent) => void, afterId = 0): () => void {
    const attempt = this.requireAttempt(ownerSessionId, attemptId);
    const firstId = attempt.events[0]?.id ?? 1;
    if (afterId > 0 && afterId < firstId - 1) {
      listener({ id: attempt.events.at(-1)?.id ?? 0, type: "auth.resync_required", snapshot: this.attemptSnapshot(attempt) });
      return () => undefined;
    }
    for (const event of attempt.events) if (event.id > afterId) listener(event);
    attempt.listeners.add(listener);
    return () => attempt.listeners.delete(listener);
  }

  async logout(providerId: string): Promise<void> {
    if (this.disconnecting.has(providerId)) throw appError("This account is already disconnecting.", 409, "logout_in_progress");
    this.disconnecting.add(providerId);
    try { await this.models.logout(providerId); }
    finally { this.disconnecting.delete(providerId); }
  }

  private async runAttempt(attempt: AuthAttempt, type: AuthType): Promise<void> {
    try {
      await this.models.login(attempt.providerId, type, {
        signal: attempt.controller.signal,
        prompt: (prompt) => this.prompt(attempt, prompt),
        notify: (event) => this.notify(attempt, event),
      });
      if (attempt.state === "cancelled" || attempt.state === "expired") return;
      attempt.state = "succeeded";
      delete attempt.prompt;
      this.emit(attempt, "auth.succeeded");
      this.retainTerminal(attempt);
    } catch (error) {
      if (attempt.state === "cancelled" || attempt.state === "expired") return;
      attempt.state = "failed";
      attempt.error = error instanceof ModelsError && error.code === "auth" && this.credentialPath
        ? `Sign-in finished, but OpenMuse could not save it. Check permissions for '${this.credentialPath}', then try again.`
        : sanitizeModelAccessError(error).message;
      delete attempt.prompt;
      this.emit(attempt, "auth.failed");
      this.retainTerminal(attempt);
    }
  }

  private prompt(attempt: AuthAttempt, prompt: AuthPrompt): Promise<string> {
    if (attempt.prompt) attempt.prompt.reject(new Error("Sign-in question was replaced."));
    return new Promise<string>((resolvePrompt, rejectPrompt) => {
      const id = randomBytes(18).toString("base64url");
      const pending: PendingPrompt = { id, prompt, resolve: resolvePrompt, reject: rejectPrompt };
      attempt.prompt = pending;
      attempt.state = "waiting_for_input";
      const onAbort = () => {
        if (attempt.prompt?.id !== id) return;
        delete attempt.prompt;
        rejectPrompt(new Error("Sign-in question was cancelled."));
        this.emit(attempt, "auth.prompt_cancelled");
      };
      prompt.signal?.addEventListener("abort", onAbort, { once: true });
      this.emit(attempt, "auth.prompt");
    });
  }

  private notify(attempt: AuthAttempt, event: AuthEvent): void {
    if (event.type === "auth_url") {
      attempt.authUrl = safeHttpsUrl(event.url);
      attempt.message = safeText(event.instructions);
      attempt.state = "waiting_for_browser";
    } else if (event.type === "device_code") {
      attempt.deviceCode = { userCode: event.userCode.slice(0, 200), verificationUri: safeHttpsUrl(event.verificationUri) };
      attempt.state = "waiting_for_device";
      if (event.expiresInSeconds) {
        attempt.expiresAt = Date.now() + Math.min(event.expiresInSeconds * 1_000, ATTEMPT_MAX_MS);
        this.armExpiry(attempt);
      }
    } else {
      attempt.message = safeText(event.message);
    }
    this.emit(attempt, `auth.${event.type}`);
  }

  private armExpiry(attempt: AuthAttempt): void {
    if (attempt.terminalTimer) clearTimeout(attempt.terminalTimer);
    attempt.terminalTimer = setTimeout(() => {
      if (this.terminal(attempt.state)) return;
      attempt.state = "expired";
      attempt.controller.abort(new Error("Sign-in expired."));
      attempt.prompt?.reject(new Error("Sign-in expired."));
      delete attempt.prompt;
      this.emit(attempt, "auth.expired");
      this.retainTerminal(attempt);
    }, Math.max(1, attempt.expiresAt - Date.now()));
    attempt.terminalTimer.unref();
  }

  private retainTerminal(attempt: AuthAttempt): void {
    if (attempt.terminalTimer) clearTimeout(attempt.terminalTimer);
    attempt.terminalTimer = setTimeout(() => this.attempts.delete(attempt.id), TERMINAL_RETENTION_MS);
    attempt.terminalTimer.unref();
  }

  private emit(attempt: AuthAttempt, type: string): void {
    const event = { id: (attempt.events.at(-1)?.id ?? 0) + 1, type, snapshot: this.attemptSnapshot(attempt) };
    attempt.events.push(event);
    if (attempt.events.length > ATTEMPT_EVENT_LIMIT) attempt.events.splice(0, attempt.events.length - ATTEMPT_EVENT_LIMIT);
    for (const listener of attempt.listeners) listener(event);
  }

  private attemptSnapshot(attempt: AuthAttempt): AuthAttemptSnapshot {
    const prompt = attempt.prompt?.prompt;
    return {
      id: attempt.id, providerId: attempt.providerId, state: attempt.state,
      createdAt: new Date(attempt.createdAt).toISOString(), expiresAt: new Date(attempt.expiresAt).toISOString(),
      ...(prompt ? { prompt: {
        id: attempt.prompt!.id, type: prompt.type, message: safeText(prompt.message) ?? "Continue sign-in.",
        ...(prompt.type !== "select" && prompt.placeholder ? { placeholder: safeText(prompt.placeholder, 500) } : {}),
        ...(prompt.type === "select" ? { options: prompt.options.slice(0, 20).map((option) => ({ id: option.id.slice(0, 80), label: option.label.slice(0, 200), description: safeText(option.description, 500) })) } : {}),
      } } : {}),
      ...(attempt.authUrl ? { authUrl: attempt.authUrl } : {}),
      ...(attempt.deviceCode ? { deviceCode: { ...attempt.deviceCode } } : {}),
      ...(attempt.message ? { message: attempt.message } : {}),
      ...(attempt.error ? { error: attempt.error } : {}),
    };
  }

  private requireAttempt(ownerSessionId: string, attemptId: string): AuthAttempt {
    const attempt = this.attempts.get(attemptId);
    if (!attempt || attempt.ownerSessionId !== ownerSessionId) throw appError("That sign-in attempt was not found. Start sign-in again.", 404, "attempt_not_found");
    return attempt;
  }

  private terminal(state: AuthAttemptState): boolean {
    return ["succeeded", "expired", "cancelled", "failed"].includes(state);
  }
}
