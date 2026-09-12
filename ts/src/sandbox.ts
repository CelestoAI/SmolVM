import { createReadStream } from "node:fs";
import { mkdir, readFile, rename, stat, unlink, writeFile } from "node:fs/promises";
import { basename, dirname, join } from "node:path";
import { randomUUID } from "node:crypto";
import { SmolVMError } from "./errors.js";
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

function sandboxPath(path: string): string {
  if (!path.startsWith("/")) {
    throw new SmolVMError("invalid_path", "Sandbox paths must be absolute and start with '/'.", {
      operation: "files.path",
      actual: { path },
    });
  }
  return path;
}

function quoteArg(value: string): string {
  if (value.length === 0) return "''";
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

class Files implements SandboxFiles {
  constructor(private readonly sandbox: Sandbox) {}

  async read(path: string): Promise<string> {
    const bytes = await this.sandbox.readBytes(sandboxPath(path));
    return new TextDecoder().decode(bytes);
  }

  async write(path: string, content: string | Uint8Array): Promise<void> {
    const bytes = typeof content === "string" ? new TextEncoder().encode(content) : content;
    await this.sandbox.writeBytes(sandboxPath(path), bytes);
  }

  async upload(localPath: string, targetPath: string): Promise<void> {
    const requestStream = this.sandbox.streamUpload;
    if (!requestStream) {
      await this.write(targetPath, await readFile(localPath));
      return;
    }
    const metadata = await stat(localPath);
    await requestStream(sandboxPath(targetPath), createReadStream(localPath), metadata.size);
  }

  async download(sourcePath: string, localPath: string): Promise<void> {
    const bytes = await this.sandbox.readBytes(sandboxPath(sourcePath));
    const parent = dirname(localPath);
    await mkdir(parent, { recursive: true });
    const temporary = join(parent, `.${basename(localPath)}.smolvm-${randomUUID()}.tmp`);
    try {
      await writeFile(temporary, bytes);
      await rename(temporary, localPath);
    } finally {
      await unlink(temporary).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== "ENOENT") throw error;
      });
    }
  }
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
    private readonly debug: boolean,
  ) {
    this.id = id;
    this.currentStatus = status;
    this.files = new Files(this);
  }

  /** @internal */
  static create(
    id: string,
    status: SandboxStatus,
    transport: SmolVMTransport,
    emit: (event: SmolVMEvent) => void,
    release: (sandbox: Sandbox) => void,
    debug: boolean,
  ): Sandbox {
    return new Sandbox(id, status, transport, emit, release, debug);
  }

  get status(): SandboxStatus {
    return this.currentStatus;
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
        let sessionClosed = false;
        if (!sandboxDeleted) {
          await this.transport.close();
          sessionClosed = true;
        }
        this.currentStatus = "deleted";
        this.release(this);
        this.emit({ type: "sandbox.deleted", sandboxId: this.id });
        throw new SmolVMError(
          "command_timeout",
          sandboxDeleted
            ? `Command timed out and sandbox '${this.id}' was deleted to confirm it stopped.`
            : "Command timed out and the SDK session was closed to confirm it stopped.",
          {
            operation: "sandbox.exec",
            sandboxId: this.id,
            actual: { sandboxDeleted, sessionClosed },
            cause,
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
        await this.transport.close();
        sessionClosed = true;
      }
      this.currentStatus = "deleted";
      this.release(this);
      throw new SmolVMError("command_aborted", sandboxDeleted
        ? `Command was aborted and sandbox '${this.id}' was deleted to confirm it stopped.`
        : "Command was aborted and the SDK session was closed to confirm it stopped.", {
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
    }).then(() => {
      this.currentStatus = "deleted";
      this.release(this);
      this.emit({ type: "sandbox.deleted", sandboxId: this.id });
    }).catch((cause) => {
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

  /** @internal */
  async readBytes(path: string): Promise<Uint8Array> {
    return this.transport.requestBytes(
      `/sandboxes/${encodeURIComponent(this.id)}/files?path=${encodeURIComponent(path)}`,
    );
  }

  /** @internal */
  async writeBytes(path: string, content: Uint8Array): Promise<void> {
    const body = new ArrayBuffer(content.byteLength);
    new Uint8Array(body).set(content);
    await this.transport.request<void>(
      `/sandboxes/${encodeURIComponent(this.id)}/files?path=${encodeURIComponent(path)}`,
      { method: "PUT", headers: { "content-type": "application/octet-stream" }, body },
    );
  }

  /** @internal */
  get streamUpload(): ((path: string, content: AsyncIterable<Uint8Array>, size: number) => Promise<void>) | undefined {
    if (!this.transport.requestStream) return undefined;
    return (path, content, size) => this.transport.requestStream!(
      `/sandboxes/${encodeURIComponent(this.id)}/files?path=${encodeURIComponent(path)}`,
      content,
      size,
    );
  }
}
