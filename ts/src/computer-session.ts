import { SmolVMError } from "./errors.js";
import { RemoteFiles } from "./remote-files.js";
import type {
  ComputerBrowserClient,
  ComputerBrowserStatus,
  ComputerDisplayClient,
  ComputerSessionClient,
  ComputerSessionStatus,
  ExecOptions,
  ExecResult,
  SandboxFiles,
  SmolVMEvent,
  SmolVMTransport,
} from "./types.js";
import type { ExecResponse } from "./client/types.gen.js";

function quoteArg(value: string): string {
  if (value.length === 0) return "''";
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

export interface ComputerResponse {
  computer_id: string;
  sandbox_id: string;
  template: "linux-desktop";
  status: "ready";
  capabilities: string[];
  display: { viewer_url: string; vnc_url: string };
  browser: { status: ComputerBrowserStatus; cdp_url?: string | null };
}

interface ComputerBrowserResponse {
  status: ComputerBrowserStatus;
  cdp_url?: string | null;
}

class ComputerBrowser implements ComputerBrowserClient {
  private currentStatus: ComputerBrowserStatus;
  private currentCdpUrl: string | null;

  constructor(
    wire: ComputerBrowserResponse,
    private readonly computer: ComputerSession,
    private readonly transport: SmolVMTransport,
  ) {
    this.currentStatus = wire.status;
    this.currentCdpUrl = wire.cdp_url ?? null;
  }

  get status(): ComputerBrowserStatus {
    return this.currentStatus;
  }

  get cdpUrl(): string | null {
    return this.currentCdpUrl;
  }

  async launch(): Promise<void> {
    this.computer.assertReady("computer.browser.launch");
    const wire = await this.transport.request<ComputerBrowserResponse>(
      `/computers/${encodeURIComponent(this.computer.computerId)}/browser/launch`,
      { method: "POST" },
    );
    this.currentStatus = wire.status;
    this.currentCdpUrl = wire.cdp_url ?? null;
  }
}

/** A complete Linux desktop with grouped display, browser, file, and command access. */
export class ComputerSession implements ComputerSessionClient {
  readonly computerId: string;
  readonly sandboxId: string;
  readonly template = "linux-desktop" as const;
  readonly capabilities: readonly string[];
  readonly display: ComputerDisplayClient;
  readonly browser: ComputerBrowserClient;
  readonly files: SandboxFiles;
  private currentStatus: ComputerSessionStatus;
  private deletePromise?: Promise<void>;

  private constructor(
    wire: ComputerResponse,
    private readonly transport: SmolVMTransport,
    private readonly emit: (event: SmolVMEvent) => void,
    private readonly release: (computer: ComputerSession) => void,
    private readonly closeSession: () => Promise<void>,
    private readonly debug: boolean,
  ) {
    if (wire.status !== "ready" || !wire.display.viewer_url || !wire.display.vnc_url) {
      throw new SmolVMError(
        "computer_endpoint_unavailable",
        `Computer '${wire.computer_id}' did not return a ready display; call smolvm.computers.create() to create a replacement.`,
        { operation: "computer.create", sandboxId: wire.sandbox_id },
      );
    }
    this.computerId = wire.computer_id;
    this.sandboxId = wire.sandbox_id;
    this.currentStatus = wire.status;
    this.capabilities = Object.freeze([...wire.capabilities]);
    this.display = Object.freeze({
      viewerUrl: wire.display.viewer_url,
      vncUrl: wire.display.vnc_url,
    });
    this.browser = new ComputerBrowser(wire.browser, this, transport);
    this.files = new RemoteFiles(
      transport,
      `/computers/${encodeURIComponent(this.computerId)}`,
      `Computer '${this.computerId}' file`,
      () => this.assertReady("computer.files.access"),
    );
  }

  /** @internal */
  static create(
    wire: ComputerResponse,
    transport: SmolVMTransport,
    emit: (event: SmolVMEvent) => void,
    release: (computer: ComputerSession) => void,
    closeSession: () => Promise<void>,
    debug: boolean,
  ): ComputerSession {
    return new ComputerSession(wire, transport, emit, release, closeSession, debug);
  }

  get status(): ComputerSessionStatus {
    return this.currentStatus;
  }

  /** @internal */
  assertReady(operation: string): void {
    if (this.currentStatus !== "ready") {
      throw new SmolVMError(
        "computer_deleted",
        `Computer '${this.computerId}' is not ready; call smolvm.computers.create() to create a replacement.`,
        { operation, sandboxId: this.sandboxId },
      );
    }
  }

  async exec(command: string | readonly string[], options: ExecOptions = {}): Promise<ExecResult> {
    this.assertReady("computer.exec");
    if (Array.isArray(command) && command.length === 0) {
      throw new TypeError("Command argv must contain at least one item.");
    }
    const normalized = typeof command === "string" ? command : command.map(quoteArg).join(" ");
    const timeoutMs = options.timeoutMs ?? 30_000;
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 3_600_000) {
      throw new RangeError("timeoutMs must be an integer from 1 to 3,600,000.");
    }
    this.emit({ type: "command.started", sandboxId: this.sandboxId });
    try {
      const wire = await this.transport.request<ExecResponse>(
        `/computers/${encodeURIComponent(this.computerId)}/exec`,
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
      if (cause instanceof SmolVMError && cause.code === "command_timeout") {
        const computerDeleted = cause.actual?.sandboxDeleted === true;
        const cleanup = computerDeleted
          ? { sessionClosed: false }
          : await this.closeSessionToConfirmStop();
        if (computerDeleted) this.markDeleted();
        throw new SmolVMError(
          "command_timeout",
          computerDeleted
            ? `Command timed out and computer '${this.computerId}' was deleted to confirm it stopped.`
            : cleanup.sessionClosed
              ? "Command timed out and the SDK session was closed to confirm it stopped."
              : "Command timed out, but SmolVM could not confirm that it stopped; call smolvm.close() again.",
          {
            operation: "computer.exec",
            sandboxId: this.sandboxId,
            actual: { computerDeleted, sessionClosed: cleanup.sessionClosed },
            cause: cleanup.cleanupCause === undefined
              ? cause
              : new AggregateError([cause, cleanup.cleanupCause], "Command timeout cleanup failed"),
            debug: this.debug,
          },
        );
      }
      const aborted = options.signal?.aborted || (cause as { name?: string })?.name === "AbortError";
      if (!aborted) throw cause;
      let computerDeleted = false;
      let sessionClosed = false;
      try {
        await this.transport.request<void>(
          `/computers/${encodeURIComponent(this.computerId)}/cancel`,
          { method: "POST" },
        );
        computerDeleted = true;
      } catch {
        const cleanup = await this.closeSessionToConfirmStop();
        sessionClosed = cleanup.sessionClosed;
      }
      if (computerDeleted) this.markDeleted();
      throw new SmolVMError(
        "command_aborted",
        computerDeleted
          ? `Command was aborted and computer '${this.computerId}' was deleted to confirm it stopped.`
          : sessionClosed
            ? "Command was aborted and the SDK session was closed to confirm it stopped."
            : "Command was aborted, but SmolVM could not confirm that it stopped; call smolvm.close() again.",
        {
          operation: "computer.exec",
          sandboxId: this.sandboxId,
          actual: { computerDeleted, sessionClosed },
          cause,
          debug: this.debug,
        },
      );
    }
  }

  /** @internal */
  markDeleted(): void {
    if (this.currentStatus === "deleted") return;
    this.currentStatus = "deleted";
    this.release(this);
    this.emit({
      type: "computer.deleted",
      computerId: this.computerId,
      sandboxId: this.sandboxId,
    });
  }

  private async closeSessionToConfirmStop(): Promise<{
    sessionClosed: boolean;
    cleanupCause?: unknown;
  }> {
    try {
      await this.closeSession();
      return { sessionClosed: true };
    } catch (cleanupCause) {
      return { sessionClosed: false, cleanupCause };
    }
  }

  async delete(): Promise<void> {
    if (this.deletePromise) return this.deletePromise;
    if (this.currentStatus === "deleted") return;
    this.currentStatus = "stopping";
    this.emit({
      type: "computer.stopping",
      computerId: this.computerId,
      sandboxId: this.sandboxId,
    });
    this.deletePromise = this.transport.request<void>(
      `/computers/${encodeURIComponent(this.computerId)}`,
      { method: "DELETE" },
    ).then(() => this.markDeleted()).catch((cause) => {
      this.currentStatus = "error";
      this.deletePromise = undefined;
      throw cause;
    });
    return this.deletePromise;
  }
}
