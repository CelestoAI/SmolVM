import process from "node:process";
import { SmolVMError } from "./errors.js";
import { Sandbox } from "./sandbox.js";
import { ProcessTransport } from "./transport.js";
import type {
  CreateSandboxOptions,
  DiagnoseResult,
  SandboxCollection,
  SandboxStatus,
  SmolVMClient,
  SmolVMEvent,
  SmolVMOptions,
  SmolVMTransport,
} from "./types.js";

export { SmolVMError } from "./errors.js";
export { Sandbox } from "./sandbox.js";
export type { SmolVMErrorCode, SmolVMErrorOptions } from "./errors.js";
export type * from "./types.js";

interface WireSandbox { id: string; status: SandboxStatus }
interface WireCapabilities { protocol_version: number; capabilities: string[] }
interface WireDiagnostics {
  protocol_version: number;
  runtime_version: string;
  python_version: string;
  platform: string;
  supported: boolean;
  problems: string[];
}

const REQUIRED_CAPABILITIES = [
  "sandbox.create",
  "sandbox.delete",
  "sandbox.exec",
  "files.read",
  "files.write",
  "events",
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

/** Entry point for creating disposable local sandboxes. */
export class SmolVM implements SmolVMClient {
  readonly sandboxes: SandboxCollection;
  private readonly transport: SmolVMTransport;
  private readonly active = new Set<Sandbox>();
  private readonly onEvent?: (event: SmolVMEvent) => void;
  private readonly debug: boolean;
  private negotiation?: Promise<void>;
  private closePromise?: Promise<void>;

  constructor(options: SmolVMOptions = {}) {
    assertSupportedNode();
    this.onEvent = options.onEvent;
    this.debug = options.debug ?? false;
    this.transport = options.transport ?? new ProcessTransport(
      options.runtimePath ?? process.env.SMOLVM_RUNTIME ?? "smolvm",
      options.startupTimeoutMs ?? 30_000,
      this.debug,
      (event) => this.emit(event),
    );
    this.sandboxes = { create: (createOptions) => this.createSandbox(createOptions) };
  }

  private emit(event: SmolVMEvent): void {
    try { this.onEvent?.(event); } catch { /* Lifecycle observers never change VM behavior. */ }
  }

  private async negotiate(): Promise<void> {
    if (!this.negotiation) {
      this.negotiation = this.transport.request<WireCapabilities>("/sdk/v1/capabilities").then((result) => {
        const missing = REQUIRED_CAPABILITIES.filter((capability) => !result.capabilities.includes(capability));
        if (result.protocol_version !== 1 || missing.length > 0) {
          throw new SmolVMError("protocol_incompatible", "The installed SmolVM runtime is incompatible with this SDK.", {
            operation: "runtime.negotiate",
            actual: { protocolVersion: result.protocol_version, missingCapabilities: missing.join(",") },
            recoveryCommand: "curl -sSL https://celesto.ai/install.sh | bash",
          });
        }
      });
    }
    return this.negotiation;
  }

  private async createSandbox(options: CreateSandboxOptions = {}): Promise<Sandbox> {
    await this.negotiate();
    this.emit({ type: "sandbox.starting" });
    const network = options.network?.mode === "restricted"
      ? { mode: "restricted", allowed_cidrs: options.network.allowedCidrs }
      : options.network ?? { mode: "open" };
    const wire = await this.transport.request<WireSandbox>("/sandboxes", {
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
    const sandbox = Sandbox.create(
      wire.id,
      wire.status,
      this.transport,
      (event) => this.emit(event),
      (released) => this.active.delete(released),
      this.debug,
    );
    this.active.add(sandbox);
    this.emit({ type: "sandbox.ready", sandboxId: sandbox.id });
    return sandbox;
  }

  async diagnose(): Promise<DiagnoseResult> {
    await this.negotiate();
    const wire = await this.transport.request<WireDiagnostics>("/sdk/v1/diagnostics");
    return {
      protocolVersion: wire.protocol_version,
      runtimeVersion: wire.runtime_version,
      nodeVersion: process.versions.node,
      pythonVersion: wire.python_version,
      platform: wire.platform,
      supported: wire.supported,
      problems: wire.problems,
    };
  }

  async close(): Promise<void> {
    if (this.closePromise) return this.closePromise;
    this.closePromise = (async () => {
      const failures: unknown[] = [];
      await Promise.all([...this.active].map(async (sandbox) => {
        try { await sandbox.delete(); } catch (cause) { failures.push(cause); }
      }));
      try { await this.transport.close(); } catch (cause) { failures.push(cause); }
      this.active.clear();
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
