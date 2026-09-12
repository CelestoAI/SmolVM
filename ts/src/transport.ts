import { spawn, type ChildProcess } from "node:child_process";
import { randomBytes } from "node:crypto";
import { SmolVMError, type SmolVMErrorCode } from "./errors.js";
import type { SmolVMEvent, SmolVMTransport } from "./types.js";

interface ReadyRecord {
  type: "smolvm.sdk.ready";
  protocol_version: number;
  host: string;
  port: number;
}

function detailFrom(body: unknown): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => (item as { msg?: string }).msg).filter(Boolean).join("; ");
    }
  }
  return "The local SmolVM runtime returned an unexpected response.";
}

function codeFor(path: string, status: number, detail: string): SmolVMErrorCode {
  if (status === 408) return "command_timeout";
  if (status === 400 && detail.toLowerCase().includes("path")) return "invalid_path";
  if (path === "/sandboxes" && detail.toLowerCase().includes("backend")) {
    return "backend_unavailable";
  }
  if (path === "/sandboxes" && detail.toLowerCase().includes("image")) {
    return "image_download_failed";
  }
  if (path === "/sandboxes") return "sandbox_create_failed";
  if (path.includes("/files") && status === 400) return "invalid_path";
  return "transport_failed";
}

export class ProcessTransport implements SmolVMTransport {
  private child?: ChildProcess;
  private control?: NodeJS.WritableStream;
  private baseUrl?: string;
  private token?: string;
  private startPromise?: Promise<void>;
  private closed = false;
  private readonly eventAbort = new AbortController();

  constructor(
    private readonly runtimePath: string,
    private readonly startupTimeoutMs: number,
    private readonly debug: boolean,
    private readonly emit: (event: SmolVMEvent) => void,
  ) {}

  private start(): Promise<void> {
    if (this.startPromise) return this.startPromise;
    this.startPromise = new Promise((resolve, reject) => {
      if (this.closed) {
        reject(new SmolVMError("bridge_exit", "The SmolVM client is already closed.", { operation: "runtime.start" }));
        return;
      }
      this.emit({ type: "runtime.starting" });
      const token = randomBytes(32).toString("base64url");
      const child = spawn(this.runtimePath, ["server", "start", "--sdk-session"], {
        stdio: ["ignore", "pipe", "pipe", "pipe"],
        windowsHide: true,
      });
      this.child = child;
      this.token = token;
      const control = child.stdio[3];
      if (!control || typeof (control as NodeJS.WritableStream).write !== "function") {
        reject(new SmolVMError("bridge_exit", "SmolVM could not open its private control pipe.", { operation: "runtime.start" }));
        return;
      }
      this.control = control as NodeJS.WritableStream;
      this.control.write(`${JSON.stringify({ protocol_version: 1, token })}\n`);

      let settled = false;
      let stdout = "";
      let stderr = "";
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        child.kill();
        reject(new SmolVMError("bridge_exit", "The local SmolVM runtime did not become ready in time.", {
          operation: "runtime.start",
          actual: { startupTimeoutMs: this.startupTimeoutMs },
          recoveryCommand: "smolvm doctor --strict",
        }));
      }, this.startupTimeoutMs);
      child.stderr?.on("data", (chunk: Buffer) => {
        stderr = (stderr + chunk.toString("utf8")).slice(-8_192);
      });
      child.stdout?.on("data", (chunk: Buffer) => {
        if (settled) return;
        stdout += chunk.toString("utf8");
        const newline = stdout.indexOf("\n");
        if (newline < 0) return;
        try {
          const record = JSON.parse(stdout.slice(0, newline)) as ReadyRecord;
          if (record.type !== "smolvm.sdk.ready" || record.protocol_version !== 1) {
            throw new SmolVMError("protocol_incompatible", "The installed SmolVM runtime uses an incompatible SDK protocol.", {
              operation: "runtime.negotiate",
              actual: { protocolVersion: record.protocol_version },
              recoveryCommand: "curl -sSL https://celesto.ai/install.sh | bash",
            });
          }
          if (record.host !== "127.0.0.1" || !Number.isInteger(record.port)) throw new Error("invalid readiness record");
          settled = true;
          clearTimeout(timer);
          this.baseUrl = `http://${record.host}:${record.port}`;
          this.emit({ type: "runtime.ready", protocolVersion: 1 });
          void this.streamEvents();
          resolve();
        } catch (cause) {
          settled = true;
          clearTimeout(timer);
          child.kill();
          reject(cause instanceof SmolVMError ? cause : new SmolVMError("bridge_exit", "The local SmolVM runtime returned an invalid readiness record.", { operation: "runtime.start", cause, debug: this.debug }));
        }
      });
      child.once("error", (cause: NodeJS.ErrnoException) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        const missing = cause.code === "ENOENT";
        reject(new SmolVMError(missing ? "runtime_missing" : "bridge_exit", missing
          ? "SmolVM is not installed or is not on PATH."
          : "The local SmolVM runtime could not start.", {
          operation: "runtime.start",
          recoveryCommand: missing ? "curl -sSL https://celesto.ai/install.sh | bash" : "smolvm doctor --strict",
          cause,
          debug: this.debug,
        }));
      });
      child.once("exit", (exitCode) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        const dependenciesMissing = stderr.includes("server dependencies are not installed");
        reject(new SmolVMError(
          dependenciesMissing ? "runtime_missing" : "bridge_exit",
          dependenciesMissing
            ? "SmolVM is installed without its local SDK server dependencies."
            : "The local SmolVM runtime exited before it was ready.", {
          operation: "runtime.start",
          actual: { exitCode: exitCode ?? -1 },
          recoveryCommand: dependenciesMissing
            ? "curl -sSL https://celesto.ai/install.sh | bash"
            : "smolvm doctor --strict",
          cause: new Error(stderr.replaceAll(token, "[redacted]")),
          debug: this.debug,
        }));
      });
    });
    return this.startPromise;
  }

  private async streamEvents(): Promise<void> {
    while (!this.closed && this.baseUrl && this.token) {
      try {
        const response = await fetch(`${this.baseUrl}/sdk/v1/events`, {
          headers: { authorization: `Bearer ${this.token}` },
          signal: this.eventAbort.signal,
        });
        if (!response.ok || !response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let pending = "";
        while (!this.closed) {
          const { done, value } = await reader.read();
          if (done) break;
          pending += decoder.decode(value, { stream: true });
          let boundary = pending.indexOf("\n\n");
          while (boundary >= 0) {
            const frame = pending.slice(0, boundary);
            pending = pending.slice(boundary + 2);
            const data = frame.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
            if (data) {
              const event = JSON.parse(data) as SmolVMEvent;
              if (event.type === "image.download") this.emit(event);
            }
            boundary = pending.indexOf("\n\n");
          }
        }
      } catch (cause) {
        if (this.closed || (cause as { name?: string } | null)?.name === "AbortError") return;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.fetch(path, init);
    if (response.status === 204) return undefined as T;
    return await response.json() as T;
  }

  async requestBytes(path: string, init: RequestInit = {}): Promise<Uint8Array> {
    const response = await this.fetch(path, init);
    return new Uint8Array(await response.arrayBuffer());
  }

  async requestStream(
    path: string,
    content: AsyncIterable<Uint8Array>,
    contentLength: number,
  ): Promise<void> {
    const init = {
      method: "PUT",
      headers: {
        "content-type": "application/octet-stream",
        "content-length": String(contentLength),
      },
      body: content as unknown as BodyInit,
      duplex: "half",
    } as RequestInit;
    await this.fetch(path, init);
  }

  private async fetch(path: string, init: RequestInit): Promise<Response> {
    await this.start();
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: { ...init.headers, authorization: `Bearer ${this.token}` },
      });
    } catch (cause) {
      if ((cause as { name?: string })?.name === "AbortError") throw cause;
      throw new SmolVMError("bridge_exit", "The local SmolVM bridge stopped responding.", {
        operation: `${init.method ?? "GET"} ${path}`,
        recoveryCommand: "smolvm doctor --strict",
        cause,
        debug: this.debug,
      });
    }
    if (!response.ok) {
      let body: unknown;
      try { body = await response.json(); } catch { body = undefined; }
      const detail = detailFrom(body);
      throw new SmolVMError(codeFor(path, response.status, detail), detail, {
        operation: `${init.method ?? "GET"} ${path}`,
        actual: { status: response.status },
        recoveryCommand: response.status >= 500 ? "smolvm doctor --strict" : undefined,
      });
    }
    return response;
  }

  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.eventAbort.abort();
    const child = this.child;
    if (!child) return;
    (this.control as { end?: () => void } | undefined)?.end?.();
    if (child.exitCode !== null) return;
    await new Promise<void>((resolve) => {
      const timer = setTimeout(() => { child.kill(); resolve(); }, 10_000);
      child.once("exit", () => { clearTimeout(timer); resolve(); });
    });
  }
}
