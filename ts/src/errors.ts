export type SmolVMErrorCode =
  | "unsupported_node"
  | "runtime_missing"
  | "protocol_incompatible"
  | "backend_unavailable"
  | "image_download_failed"
  | "sandbox_create_failed"
  | "invalid_path"
  | "command_timeout"
  | "command_aborted"
  | "bridge_exit"
  | "cleanup_failed"
  | "transport_failed";

export interface SmolVMErrorOptions {
  operation: string;
  sandboxId?: string;
  actual?: Readonly<Record<string, string | number | boolean>>;
  recoveryCommand?: string;
  helpUrl?: string;
  cause?: unknown;
  debug?: boolean;
}

/** A stable, actionable failure from the SDK or local runtime. */
export class SmolVMError extends Error {
  readonly code: SmolVMErrorCode;
  readonly operation: string;
  readonly sandboxId?: string;
  readonly actual?: Readonly<Record<string, string | number | boolean>>;
  readonly recoveryCommand?: string;
  readonly helpUrl: string;

  constructor(code: SmolVMErrorCode, message: string, options: SmolVMErrorOptions) {
    super(message);
    this.name = "SmolVMError";
    this.code = code;
    this.operation = options.operation;
    this.sandboxId = options.sandboxId;
    this.actual = options.actual;
    this.recoveryCommand = options.recoveryCommand;
    this.helpUrl = options.helpUrl ?? `https://celesto.ai/docs/errors/${code}`;
    if (options.debug && options.cause !== undefined) {
      Object.defineProperty(this, "cause", { value: options.cause, enumerable: false });
    }
  }
}
