import type { SmolVMError } from "./errors.js";

/** The current lifecycle state of a sandbox, including local deletion. */
export type SandboxStatus =
  | "created"
  | "running"
  | "paused"
  | "stopped"
  | "error"
  | "deleted";

/** See when a browser computer is ready for commands or has stopped. A browser-session status is its current lifecycle state. */
export type BrowserSessionStatus =
  | "created"
  | "starting"
  | "ready"
  | "stopping"
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

/** Start an isolated browser computer with the resources and live view you need. Chromium is the browser engine running inside it. */
export interface CreateBrowserSessionOptions {
  sessionId?: string;
  mode?: "headless" | "live";
  backend?: "firecracker" | "qemu" | "libkrun" | "auto";
  profile?: { mode: "ephemeral" } | { mode: "persistent"; id: string };
  timeoutMinutes?: number;
  viewport?: { width: number; height: number };
  recordVideo?: boolean;
  allowDownloads?: boolean;
  memoryMiB?: number;
  diskMiB?: number;
  network?: NetworkPolicy;
}

/** Start a complete Linux desktop with a display, Chromium, files, and commands. */
export interface CreateComputerOptions {
  name?: string;
  template?: "linux-desktop";
  backend?: "firecracker" | "qemu" | "auto";
  display?: { width: number; height: number };
  resources?: { memoryMiB?: number; diskMiB?: number; vcpus?: 2 };
  network?: NetworkPolicy;
  workspace?: readonly {
    hostPath: string;
    guestPath?: string;
    writable?: boolean;
  }[];
}

/** Whether a complete desktop computer is usable or has been deleted. */
export type ComputerSessionStatus = "ready" | "stopping" | "error" | "deleted";

/** Whether Chromium inside a computer is ready for automation. */
export type ComputerBrowserStatus = "ready" | "closed" | "error";

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
  | { type: "browser.starting"; sessionId: string }
  | { type: "browser.ready"; sessionId: string; sandboxId: string }
  | { type: "browser.stopping"; sessionId: string; sandboxId: string }
  | { type: "browser.deleted"; sessionId: string; sandboxId: string }
  | { type: "computer.starting"; computerId: string }
  | { type: "computer.ready"; computerId: string; sandboxId: string }
  | { type: "computer.stopping"; computerId: string; sandboxId: string }
  | { type: "computer.deleted"; computerId: string; sandboxId: string }
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

/** Run commands and exchange files with one disposable environment. */
export interface CommandFilesClient {
  readonly files: SandboxFiles;
  exec(command: string | readonly string[], options?: ExecOptions): Promise<ExecResult>;
}

/** @deprecated Use CommandFilesClient for the shared command-and-files contract. */
export type ComputerClient = CommandFilesClient;

/** The mockable command, file, status, and deletion contract for one sandbox. */
export interface SandboxClient extends CommandFilesClient {
  readonly id: string;
  readonly status: SandboxStatus;
  delete(): Promise<void>;
}

/** Creates sandboxes owned by one SmolVM client. */
export interface SandboxCollection {
  create(options?: CreateSandboxOptions): Promise<SandboxClient>;
}

/** Control a ready browser computer through private automation and viewing addresses. An endpoint is a local address used to connect to that computer. */
export interface BrowserSessionClient extends CommandFilesClient {
  readonly sessionId: string;
  readonly sandboxId: string;
  readonly status: BrowserSessionStatus;
  readonly cdpUrl: string;
  readonly viewerUrl?: string;
  readonly displayUrl?: string;
  readonly profileId?: string;
  delete(): Promise<void>;
}

/** Create browser computers that this SmolVM client will clean up. A browser session is one isolated Chromium environment. */
export interface BrowserSessionCollection {
  create(options?: CreateBrowserSessionOptions): Promise<BrowserSessionClient>;
}

/** Addresses for watching and controlling the visible Linux desktop. */
export interface ComputerDisplayClient {
  readonly viewerUrl: string;
  readonly vncUrl: string;
}

/** Chromium included in a Linux computer, with automation available when it is open. */
export interface ComputerBrowserClient {
  readonly status: ComputerBrowserStatus;
  readonly cdpUrl: string | null;
  launch(): Promise<void>;
}

/** A complete Linux desktop grouped by display, browser, files, and commands. */
export interface ComputerSessionClient extends CommandFilesClient {
  readonly computerId: string;
  readonly sandboxId: string;
  readonly template: "linux-desktop";
  readonly status: ComputerSessionStatus;
  readonly capabilities: readonly string[];
  readonly display: ComputerDisplayClient;
  readonly browser: ComputerBrowserClient;
  delete(): Promise<void>;
}

/** Create complete desktop computers owned by one SmolVM client. */
export interface ComputerCollection {
  create(options?: CreateComputerOptions): Promise<ComputerSessionClient>;
}

/** The mockable client contract for creating sandboxes, diagnosing setup, and cleaning up. */
export interface SmolVMClient {
  readonly sandboxes: SandboxCollection;
  readonly browsers: BrowserSessionCollection;
  readonly computers: ComputerCollection;
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
  /** Time allowed to download an image and create a sandbox. */
  createTimeoutMs?: number;
  /** Time allowed for ordinary bridge requests that do not manage a VM lifecycle operation. */
  requestTimeoutMs?: number;
  /** Retain non-enumerable causes on SmolVMError instances. */
  debug?: boolean;
  /** Supply a structural transport in tests; normal applications should omit this. */
  transport?: SmolVMTransport;
}
