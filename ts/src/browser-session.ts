import { SmolVMError } from "./errors.js";
import type {
  BrowserSessionClient,
  BrowserSessionStatus,
  SmolVMEvent,
  SmolVMTransport,
} from "./types.js";

export interface BrowserSessionResponse {
  session_id: string;
  sandbox_id: string;
  status: Exclude<BrowserSessionStatus, "deleted">;
  cdp_url: string;
  viewer_url?: string | null;
  profile_id?: string | null;
}

/** A ready browser sandbox owned by one SmolVM client. */
export class BrowserSession implements BrowserSessionClient {
  readonly sessionId: string;
  readonly sandboxId: string;
  readonly cdpUrl: string;
  readonly viewerUrl?: string;
  readonly profileId?: string;
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
        `Browser session '${wire.session_id}' did not return ready automation endpoints.`,
        { operation: "browser.create", sandboxId: wire.sandbox_id },
      );
    }
    this.sessionId = wire.session_id;
    this.sandboxId = wire.sandbox_id;
    this.currentStatus = wire.status;
    this.cdpUrl = wire.cdp_url;
    this.viewerUrl = wire.viewer_url ?? undefined;
    this.profileId = wire.profile_id ?? undefined;
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
