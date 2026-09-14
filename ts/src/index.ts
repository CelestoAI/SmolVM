import process from "node:process";
import { randomUUID } from "node:crypto";
import { BrowserSession, type BrowserSessionResponse } from "./browser-session.js";
import { SmolVMError } from "./errors.js";
import { Sandbox } from "./sandbox.js";
import { ProcessTransport } from "./transport.js";
import type {
  CapabilitiesResponse,
  DiagnosticsResponse,
  SandboxResponse,
} from "./client/types.gen.js";
import type {
  CreateSandboxOptions,
  CreateBrowserSessionOptions,
  BrowserSessionCollection,
  DiagnoseResult,
  SandboxCollection,
  SmolVMClient,
  SmolVMEvent,
  SmolVMOptions,
  SmolVMTransport,
} from "./types.js";

export { SmolVMError } from "./errors.js";
export { Sandbox } from "./sandbox.js";
export { BrowserSession } from "./browser-session.js";
export type { SmolVMErrorCode, SmolVMErrorOptions } from "./errors.js";
export type * from "./types.js";

const REQUIRED_CAPABILITIES = [
  "sandbox.create",
  "sandbox.delete",
  "sandbox.exec",
  "files.read",
  "files.write",
  "events",
] as const;

const REQUIRED_BROWSER_CAPABILITIES = [
  "browser.create",
  "browser.delete",
  "browser.endpoints",
  "browser.exec",
  "browser.files",
  "browser.events",
] as const;

function assertSupportedNode(): void {
  const [major = 0, minor = 0] = process.versions.node.split(".").map(Number);
  if (major < 20 || (major === 20 && minor < 4)) {
    throw new SmolVMError("unsupported_node", "@celestoai/smolvm requires Node.js 20.4 or newer.", {
      operation: "client.create",
      actual: { nodeVersion: process.versions.node },
      recoveryCommand: "nvm install 20",
    });
  }
}

function timeoutOption(value: number | undefined, fallback: number, name: string): number {
  const timeout = value ?? fallback;
  if (!Number.isInteger(timeout) || timeout < 1 || timeout > 3_600_000) {
    throw new RangeError(`${name} must be an integer from 1 to 3,600,000.`);
  }
  return timeout;
}

/** Entry point for creating disposable local sandboxes. */
export class SmolVM implements SmolVMClient {
  readonly sandboxes: SandboxCollection;
  readonly browsers: BrowserSessionCollection;
  private readonly transport: SmolVMTransport;
  private readonly active = new Set<Sandbox>();
  private readonly activeBrowsers = new Set<BrowserSession>();
  private readonly onEvent?: (event: SmolVMEvent) => void;
  private readonly debug: boolean;
  private negotiation?: Promise<ReadonlySet<string>>;
  private closePromise?: Promise<void>;
  private transportClosePromise?: Promise<void>;
  private sessionClosed = false;

  constructor(options: SmolVMOptions = {}) {
    assertSupportedNode();
    this.onEvent = options.onEvent;
    this.debug = options.debug ?? false;
    const startupTimeoutMs = timeoutOption(options.startupTimeoutMs, 30_000, "startupTimeoutMs");
    const createTimeoutMs = timeoutOption(options.createTimeoutMs, 600_000, "createTimeoutMs");
    const requestTimeoutMs = timeoutOption(options.requestTimeoutMs, 30_000, "requestTimeoutMs");
    this.transport = options.transport ?? new ProcessTransport(
      options.runtimePath ?? process.env.SMOLVM_RUNTIME ?? "smolvm",
      startupTimeoutMs,
      createTimeoutMs,
      requestTimeoutMs,
      this.debug,
      (event) => this.emit(event),
    );
    this.sandboxes = { create: (createOptions) => this.createSandbox(createOptions) };
    this.browsers = { create: (createOptions) => this.createBrowser(createOptions) };
  }

  private emit(event: SmolVMEvent): void {
    try { this.onEvent?.(event); } catch { /* Lifecycle observers never change VM behavior. */ }
  }

  private transitionSessionClosed(): void {
    if (this.sessionClosed) return;
    this.sessionClosed = true;
    this.transportClosePromise ??= Promise.resolve();
    for (const sandbox of [...this.active]) sandbox.markDeleted();
    this.active.clear();
    for (const browser of [...this.activeBrowsers]) browser.markDeleted();
    this.activeBrowsers.clear();
  }

  private async closeTransport(): Promise<void> {
    if (!this.transportClosePromise) {
      const attempt = this.transport.close().then(() => this.transitionSessionClosed());
      this.transportClosePromise = attempt.catch((cause) => {
        this.transportClosePromise = undefined;
        throw cause;
      });
    }
    return this.transportClosePromise;
  }

  private async negotiate(required: readonly string[] = REQUIRED_CAPABILITIES): Promise<void> {
    if (!this.negotiation) {
      const attempt = this.transport.request<CapabilitiesResponse>("/sdk/v1/capabilities").then((result) => {
        const capabilities = Array.isArray(result.capabilities) ? result.capabilities : [];
        const protocolVersion = result.protocol_version ?? -1;
        if (protocolVersion !== 1) {
          throw new SmolVMError("protocol_incompatible", "The installed SmolVM runtime is incompatible with this SDK.", {
            operation: "runtime.negotiate",
            actual: { protocolVersion },
            recoveryCommand: "curl -sSL https://celesto.ai/install.sh | bash",
          });
        }
        return new Set(capabilities);
      });
      this.negotiation = attempt.catch((cause) => {
        this.negotiation = undefined;
        throw cause;
      });
    }
    const capabilities = await this.negotiation;
    const missing = required.filter((capability) => !capabilities.has(capability));
    if (missing.length > 0) {
      throw new SmolVMError("protocol_incompatible", "The installed SmolVM runtime is incompatible with this SDK.", {
        operation: "runtime.negotiate",
        actual: { protocolVersion: 1, missingCapabilities: missing.join(",") },
        recoveryCommand: "curl -sSL https://celesto.ai/install.sh | bash",
      });
    }
  }

  private async createSandbox(options: CreateSandboxOptions = {}): Promise<Sandbox> {
    await this.negotiate();
    this.emit({ type: "sandbox.starting" });
    const network = options.network?.mode === "restricted"
      ? { mode: "restricted", allowed_cidrs: options.network.allowedCidrs }
      : options.network ?? { mode: "open" };
    let wire: SandboxResponse;
    try {
      wire = await this.transport.request<SandboxResponse>("/sandboxes", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          os: options.os ?? (options.image ? undefined : "ubuntu"),
          memory: options.memoryMiB,
          disk_size: options.diskMiB,
          backend: options.backend,
          image: options.image,
          network,
        }),
      });
    } catch (cause) {
      if (cause instanceof SmolVMError && cause.actual?.sessionClosed === true) {
        this.transitionSessionClosed();
      }
      throw cause;
    }
    const sandbox = Sandbox.create(
      wire.id,
      wire.status,
      this.transport,
      (event) => this.emit(event),
      (released) => this.active.delete(released),
      () => this.closeTransport(),
      this.debug,
    );
    this.active.add(sandbox);
    this.emit({ type: "sandbox.ready", sandboxId: sandbox.id });
    return sandbox;
  }

  private async createBrowser(options: CreateBrowserSessionOptions = {}): Promise<BrowserSession> {
    await this.negotiate([...REQUIRED_CAPABILITIES, ...REQUIRED_BROWSER_CAPABILITIES]);
    const requestedSessionId = options.sessionId ?? `browser-${randomUUID().slice(0, 8)}`;
    this.emit({ type: "browser.starting", sessionId: requestedSessionId });
    const network = options.network?.mode === "restricted"
      ? { mode: "restricted", allowed_cidrs: options.network.allowedCidrs }
      : options.network ?? { mode: "open" };
    const profile = options.profile ?? { mode: "ephemeral" as const };
    let wire: BrowserSessionResponse;
    try {
      wire = await this.transport.request<BrowserSessionResponse>("/browser-sessions", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          session_id: requestedSessionId,
          mode: options.mode ?? "headless",
          backend: options.backend ?? "auto",
          profile_mode: profile.mode,
          profile_id: profile.mode === "persistent" ? profile.id : undefined,
          timeout_minutes: options.timeoutMinutes,
          viewport: options.viewport,
          record_video: options.recordVideo,
          allow_downloads: options.allowDownloads,
          memory: options.memoryMiB,
          disk_size: options.diskMiB,
          network,
        }),
      });
    } catch (cause) {
      if (cause instanceof SmolVMError && cause.actual?.sessionClosed === true) {
        this.transitionSessionClosed();
      }
      throw cause;
    }
    const browser = BrowserSession.create(
      wire,
      this.transport,
      (event) => this.emit(event),
      (released) => this.activeBrowsers.delete(released),
    );
    this.activeBrowsers.add(browser);
    this.emit({ type: "browser.ready", sessionId: browser.sessionId, sandboxId: browser.sandboxId });
    return browser;
  }

  async diagnose(): Promise<DiagnoseResult> {
    await this.negotiate();
    const wire = await this.transport.request<DiagnosticsResponse>("/sdk/v1/diagnostics");
    return {
      protocolVersion: wire.protocol_version ?? -1,
      runtimeVersion: wire.runtime_version,
      nodeVersion: process.versions.node,
      pythonVersion: wire.python_version,
      platform: wire.platform,
      supported: wire.supported,
      problems: Array.isArray(wire.problems) ? wire.problems : [],
    };
  }

  async close(): Promise<void> {
    if (this.closePromise) return this.closePromise;
    this.closePromise = (async () => {
      const failures: unknown[] = [];
      await Promise.all([...this.active].map(async (sandbox) => {
        try { await sandbox.delete(); } catch (cause) { failures.push(cause); }
      }));
      await Promise.all([...this.activeBrowsers].map(async (browser) => {
        try { await browser.delete(); } catch (cause) { failures.push(cause); }
      }));
      try { await this.closeTransport(); } catch (cause) { failures.push(cause); }
      if (failures.length > 0) {
        throw new SmolVMError("cleanup_failed", "One or more sandboxes could not be deleted; the SDK session was closed.", {
          operation: "client.close",
          actual: { failures: failures.length },
          cause: failures[0],
          debug: this.debug,
        });
      }
    })();
    return this.closePromise;
  }
}

const asyncDispose = (Symbol as typeof Symbol & { asyncDispose?: symbol }).asyncDispose;
if (asyncDispose) {
  Object.defineProperty(SmolVM.prototype, asyncDispose, {
    value(this: SmolVM) { return this.close(); },
  });
}
