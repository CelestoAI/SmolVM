import type { SmolVMError } from "./errors.js";

/** The current lifecycle state of a sandbox, including local deletion. */
export type SandboxStatus =
  | "created"
  | "running"
  | "paused"
  | "stopped"
  | "error"
  | "deleted";

/** Controls which outbound IPv4 connections a new sandbox may make. */
export type NetworkPolicy =
  | { mode: "open" }
  | { mode: "off" }
  | { mode: "restricted"; allowedCidrs: readonly string[] };

/** Configure the operating system, resources, image, and network for a new sandbox. MiB means mebibytes, a memory and disk-size unit. */
export interface CreateSandboxOptions {
  /** Guest operating system. Defaults to Ubuntu. */
  os?: "ubuntu" | "alpine";
  /** Guest memory in MiB. */
  memoryMiB?: number;
  /** Guest root disk size in MiB. */
  diskMiB?: number;
  /** Override automatic backend selection. */
  backend?: "firecracker" | "qemu" | "libkrun" | "vz";
  /** Custom local path, file URL, or remote image reference. */
  image?: string;
  /** Outbound network access. Defaults to open. */
  network?: NetworkPolicy;
}

/** Choose where and how long a command runs, plus its environment and cancellation signal. */
export interface ExecOptions {
  cwd?: string;
  env?: Readonly<Record<string, string>>;
  timeoutMs?: number;
  signal?: AbortSignal;
}

/** Captured output and exit information from a completed command. */
export interface ExecResult {
  ok: boolean;
  exitCode: number;
  stdout: string;
  stderr: string;
  durationMs: number;
}

export type SmolVMEvent =
  | { type: "runtime.starting" }
  | { type: "runtime.ready"; protocolVersion: number }
  | { type: "runtime.error"; error: SmolVMError }
  | { type: "image.download"; image: string; receivedBytes: number; totalBytes?: number }
  | { type: "sandbox.starting" }
  | { type: "sandbox.ready"; sandboxId: string }
  | { type: "sandbox.deleted"; sandboxId: string }
  | { type: "command.started"; sandboxId: string }
  | { type: "command.completed"; sandboxId: string; result: ExecResult };

/** Reports whether this SDK and the installed local runtime can work together. */
export interface DiagnoseResult {
  protocolVersion: number;
  runtimeVersion: string;
  nodeVersion: string;
  pythonVersion: string;
  platform: string;
  supported: boolean;
  problems: readonly string[];
}

/** Read, write, upload, and download files for one sandbox. */
export interface SandboxFiles {
  /** Read a UTF-8 text file from an absolute sandbox path. */
  read(path: string): Promise<string>;
  /** Write text or bytes to an absolute sandbox path. */
  write(path: string, content: string | Uint8Array): Promise<void>;
  /** Stream a host file when the transport supports it; otherwise buffer the complete file before writing it. */
  upload(localPath: string, sandboxPath: string): Promise<void>;
  /** Download to a temporary host file, then rename it atomically. */
  download(sandboxPath: string, localPath: string): Promise<void>;
}

/** The mockable command, file, status, and deletion contract for one sandbox. */
export interface SandboxClient {
  readonly id: string;
  readonly status: SandboxStatus;
  readonly files: SandboxFiles;
  exec(command: string | readonly string[], options?: ExecOptions): Promise<ExecResult>;
  delete(): Promise<void>;
}

/** Creates sandboxes owned by one SmolVM client. */
export interface SandboxCollection {
  create(options?: CreateSandboxOptions): Promise<SandboxClient>;
}

/** The mockable client contract for creating sandboxes, diagnosing setup, and cleaning up. */
export interface SmolVMClient {
  readonly sandboxes: SandboxCollection;
  diagnose(): Promise<DiagnoseResult>;
  close(): Promise<void>;
}

/** Sends private bridge requests; applications can implement it to test without a VM. */
export interface SmolVMTransport {
  request<T>(path: string, init?: RequestInit): Promise<T>;
  requestBytes(path: string, init?: RequestInit): Promise<Uint8Array>;
  requestStream?(
    path: string,
    content: AsyncIterable<Uint8Array>,
    contentLength: number,
  ): Promise<void>;
  close(): Promise<void>;
}

/** Configure runtime startup, lifecycle events, debugging, or a test transport. */
export interface SmolVMOptions {
  /** Observe typed lifecycle events. */
  onEvent?: (event: SmolVMEvent) => void;
  /** Runtime executable path. Defaults to `smolvm` on PATH. */
  runtimePath?: string;
  /** Time allowed for the local bridge to start. */
  startupTimeoutMs?: number;
  /** Time allowed for ordinary bridge requests that do not manage a VM lifecycle operation. */
  requestTimeoutMs?: number;
  /** Retain non-enumerable causes on SmolVMError instances. */
  debug?: boolean;
  /** Supply a structural transport in tests; normal applications should omit this. */
  transport?: SmolVMTransport;
}
