import { SmolVMError } from "./errors.js";
import { RemoteFiles } from "./remote-files.js";
import type { ExecResponse } from "./client/types.gen.js";
import type {
  ExecOptions,
  ExecResult,
  SandboxClient,
  SandboxFiles,
  SandboxStatus,
  SmolVMEvent,
  SmolVMTransport,
} from "./types.js";

function quoteArg(value: string): string {
  if (value.length === 0) return "''";
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

/** One disposable local computer with commands, files, status, and explicit deletion. */
export class Sandbox implements SandboxClient {
  readonly id: string;
  readonly files: SandboxFiles;
  private currentStatus: SandboxStatus;
  private deletePromise?: Promise<void>;

  private constructor(
    id: string,
    status: SandboxStatus,
    private readonly transport: SmolVMTransport,
    private readonly emit: (event: SmolVMEvent) => void,
    private readonly release: (sandbox: Sandbox) => void,
    private readonly closeSession: () => Promise<void>,
    private readonly debug: boolean,
  ) {
    this.id = id;
    this.currentStatus = status;
    this.files = new RemoteFiles(
      transport,
      `/sandboxes/${encodeURIComponent(id)}`,
      () => this.assertFilesAvailable(),
    );
  }

  /** @internal */
  static create(
    id: string,
    status: SandboxStatus,
    transport: SmolVMTransport,
    emit: (event: SmolVMEvent) => void,
    release: (sandbox: Sandbox) => void,
    closeSession: () => Promise<void>,
    debug: boolean,
  ): Sandbox {
    return new Sandbox(id, status, transport, emit, release, closeSession, debug);
  }

  get status(): SandboxStatus {
    return this.currentStatus;
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

  /** @internal */
  markDeleted(): void {
    if (this.currentStatus === "deleted") return;
    this.currentStatus = "deleted";
    this.release(this);
    this.emit({ type: "sandbox.deleted", sandboxId: this.id });
  }

  async exec(command: string | readonly string[], options: ExecOptions = {}): Promise<ExecResult> {
    if (this.currentStatus === "deleted") {
      throw new SmolVMError("transport_failed", `Sandbox '${this.id}' has been deleted.`, {
        operation: "sandbox.exec",
        sandboxId: this.id,
      });
    }
    if (Array.isArray(command) && command.length === 0) {
      throw new TypeError("Command argv must contain at least one item.");
    }
    const normalized = typeof command === "string" ? command : command.map(quoteArg).join(" ");
    const timeoutMs = options.timeoutMs ?? 30_000;
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 3_600_000) {
      throw new RangeError("timeoutMs must be an integer from 1 to 3,600,000.");
    }
    this.emit({ type: "command.started", sandboxId: this.id });
    try {
      const wire = await this.transport.request<ExecResponse>(`/sandboxes/${encodeURIComponent(this.id)}/exec`, {
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
      });
      const result: ExecResult = {
        ok: wire.exit_code === 0,
        exitCode: wire.exit_code,
        stdout: wire.stdout,
        stderr: wire.stderr,
        durationMs: wire.duration_ms ?? 0,
      };
      this.emit({ type: "command.completed", sandboxId: this.id, result });
      return result;
    } catch (cause) {
      if (cause instanceof SmolVMError && cause.code === "command_timeout") {
        const sandboxDeleted = cause.actual?.sandboxDeleted === true;
        const cleanup = sandboxDeleted
          ? { sessionClosed: false }
          : await this.closeSessionToConfirmStop();
        if (sandboxDeleted) this.markDeleted();
        throw new SmolVMError(
          "command_timeout",
          sandboxDeleted
            ? `Command timed out and sandbox '${this.id}' was deleted to confirm it stopped.`
            : cleanup.sessionClosed
              ? "Command timed out and the SDK session was closed to confirm it stopped."
              : "Command timed out, but SmolVM could not confirm that it stopped; call smolvm.close() again.",
          {
            operation: "sandbox.exec",
            sandboxId: this.id,
            actual: { sandboxDeleted, sessionClosed: cleanup.sessionClosed },
            cause: cleanup.cleanupCause === undefined
              ? cause
              : new AggregateError([cause, cleanup.cleanupCause], "Command timeout cleanup failed"),
            debug: this.debug,
          },
        );
      }
      const aborted = options.signal?.aborted || (cause as { name?: string })?.name === "AbortError";
      if (!aborted) throw cause;
      let sandboxDeleted = false;
      let sessionClosed = false;
      try {
        await this.transport.request<void>(`/sandboxes/${encodeURIComponent(this.id)}/cancel`, { method: "POST" });
        sandboxDeleted = true;
      } catch {
        const cleanup = await this.closeSessionToConfirmStop();
        sessionClosed = cleanup.sessionClosed;
      }
      if (sandboxDeleted) this.markDeleted();
      throw new SmolVMError("command_aborted", sandboxDeleted
        ? `Command was aborted and sandbox '${this.id}' was deleted to confirm it stopped.`
        : sessionClosed
          ? "Command was aborted and the SDK session was closed to confirm it stopped."
          : "Command was aborted, but SmolVM could not confirm that it stopped; call smolvm.close() again.", {
        operation: "sandbox.exec",
        sandboxId: this.id,
        actual: { sandboxDeleted, sessionClosed },
        cause,
        debug: this.debug,
      });
    }
  }

  async delete(): Promise<void> {
    if (this.currentStatus === "deleted") return;
    if (this.deletePromise) return this.deletePromise;
    this.deletePromise = this.transport.request<void>(`/sandboxes/${encodeURIComponent(this.id)}`, {
      method: "DELETE",
    }).then(() => this.markDeleted()).catch((cause) => {
      throw new SmolVMError("cleanup_failed", `Sandbox '${this.id}' could not be deleted; call smolvm.close() to end the complete session.`, {
        operation: "sandbox.delete",
        sandboxId: this.id,
        cause,
        debug: this.debug,
      });
    }).finally(() => {
      if (this.currentStatus !== "deleted") this.deletePromise = undefined;
    });
    return this.deletePromise;
  }

  private assertFilesAvailable(): void {
    if (this.currentStatus === "deleted") {
      throw new SmolVMError("transport_failed", `Sandbox '${this.id}' has been deleted.`, {
        operation: "files.access",
        sandboxId: this.id,
      });
    }
  }
}
