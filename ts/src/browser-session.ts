import { SmolVMError } from "./errors.js";
import { RemoteFiles } from "./remote-files.js";
import type {
  BrowserSessionClient,
  BrowserSessionStatus,
  ExecOptions,
  ExecResult,
  SmolVMEvent,
  SmolVMTransport,
  SandboxFiles,
} from "./types.js";
import type { ExecResponse } from "./client/types.gen.js";

function quoteArg(value: string): string {
  if (value.length === 0) return "''";
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

export interface BrowserSessionResponse {
  session_id: string;
  sandbox_id: string;
  status: Exclude<BrowserSessionStatus, "deleted">;
  cdp_url: string;
  viewer_url?: string | null;
  display_url?: string | null;
  profile_id?: string | null;
}

/** Run commands in an isolated browser computer owned by one SmolVM client. A browser session is the disposable Chromium environment and its private connection endpoints. */
export class BrowserSession implements BrowserSessionClient {
  readonly sessionId: string;
  readonly sandboxId: string;
  readonly cdpUrl: string;
  readonly viewerUrl?: string;
  readonly displayUrl?: string;
  readonly profileId?: string;
  readonly files: SandboxFiles;
  private currentStatus: BrowserSessionStatus;
  private deletePromise?: Promise<void>;

  private constructor(
    wire: BrowserSessionResponse,
    private readonly transport: SmolVMTransport,
    private readonly emit: (event: SmolVMEvent) => void,
    private readonly release: (browser: BrowserSession) => void,
  ) {
    if (wire.status !== "ready" || !wire.cdp_url) {
      throw new SmolVMError(
        "browser_endpoint_unavailable",
        `Browser session '${wire.session_id}' did not return ready automation endpoints; call smolvm.browsers.create() to create a replacement.`,
        { operation: "browser.create", sandboxId: wire.sandbox_id },
      );
    }
    this.sessionId = wire.session_id;
    this.sandboxId = wire.sandbox_id;
    this.currentStatus = wire.status;
    this.cdpUrl = wire.cdp_url;
    this.viewerUrl = wire.viewer_url ?? undefined;
    this.displayUrl = wire.display_url ?? undefined;
    this.profileId = wire.profile_id ?? undefined;
    this.files = new RemoteFiles(
      transport,
      `/browser-sessions/${encodeURIComponent(this.sessionId)}`,
      `Browser session '${this.sessionId}' file`,
      () => this.assertReady("files.access"),
    );
  }

  /** @internal */
  static create(
    wire: BrowserSessionResponse,
    transport: SmolVMTransport,
    emit: (event: SmolVMEvent) => void,
    release: (browser: BrowserSession) => void,
  ): BrowserSession {
    return new BrowserSession(wire, transport, emit, release);
  }

  get status(): BrowserSessionStatus {
    return this.currentStatus;
  }

  async exec(command: string | readonly string[], options: ExecOptions = {}): Promise<ExecResult> {
    this.assertReady("browser.exec");
    if (Array.isArray(command) && command.length === 0) throw new TypeError("Command argv must contain at least one item.");
    const normalized = typeof command === "string" ? command : command.map(quoteArg).join(" ");
    const timeoutMs = options.timeoutMs ?? 30_000;
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 3_600_000) {
      throw new RangeError("timeoutMs must be an integer from 1 to 3,600,000.");
    }
    this.emit({ type: "command.started", sandboxId: this.sandboxId });
    try {
      const wire = await this.transport.request<ExecResponse>(
        `/browser-sessions/${encodeURIComponent(this.sessionId)}/exec`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            command: normalized,
            shell: typeof command === "string" ? "login" : "raw",
            timeout: Math.ceil(timeoutMs / 1000),
            cwd: options.cwd,
            env: options.env ?? {},
          }),
          signal: options.signal,
        },
      );
      const result: ExecResult = {
        ok: wire.exit_code === 0,
        exitCode: wire.exit_code,
        stdout: wire.stdout,
        stderr: wire.stderr,
        durationMs: wire.duration_ms ?? 0,
      };
      this.emit({ type: "command.completed", sandboxId: this.sandboxId, result });
      return result;
    } catch (cause) {
      if (
        cause instanceof SmolVMError
        && cause.code === "command_timeout"
        && cause.actual?.sandboxDeleted === true
      ) {
        this.markDeleted();
      }
      throw cause;
    }
  }

  private assertReady(operation: string): void {
    if (this.currentStatus !== "ready") {
      throw new SmolVMError(
        "browser_deleted",
        `Browser session '${this.sessionId}' is not ready; call smolvm.browsers.create() to create a replacement.`,
        { operation, sandboxId: this.sandboxId },
      );
    }
  }

  /** @internal */
  markDeleted(): void {
    if (this.currentStatus === "deleted") return;
    this.currentStatus = "deleted";
    this.release(this);
    this.emit({ type: "browser.deleted", sessionId: this.sessionId, sandboxId: this.sandboxId });
  }

  async delete(): Promise<void> {
    if (this.deletePromise) return this.deletePromise;
    if (this.currentStatus === "deleted") return;
    this.currentStatus = "stopping";
    this.emit({ type: "browser.stopping", sessionId: this.sessionId, sandboxId: this.sandboxId });
    this.deletePromise = this.transport.request<void>(
      `/browser-sessions/${encodeURIComponent(this.sessionId)}`,
      { method: "DELETE" },
    ).then(() => this.markDeleted()).catch((cause) => {
      this.currentStatus = "error";
      this.deletePromise = undefined;
      throw cause;
    });
    return this.deletePromise;
  }
}
