import type { Browser, Page } from "playwright-core";
import type { Agent } from "@earendil-works/pi-agent-core";
import type { ComputerSessionClient, SmolVMClient } from "@celestoai/smolvm";
import type { StorefrontController } from "./storefront.js";
import type { BrowserOperation } from "./browser-operations.js";

export type ControlOwner = "agent" | "pause_requested" | "human";
export type RunState = "idle" | "model_turn" | "tool_action" | "waiting_for_approval" | "interrupted" | "stopping" | "stopped" | "failed";
export type SessionLifecycle = "absent" | "starting" | "ready" | "stopping" | "deleted" | "error";

export interface Message { id: string; role: "user" | "assistant"; text: string; createdAt: string }
export interface ConversationEvent {
  id: number; conversationId: string; stateVersion: number; createdAt: string; type: string;
  payload: Record<string, unknown>;
}
export interface IntentGrant {
  id: string; actionKind: "add_to_cart"; subject: { productId: string } | { categoryId: string };
  variant: { kind: "any" } | { kind: "exact"; id: string }; maxQuantity: 1;
  maxUnitPriceMinor: number; currency: "INR"; sourceMessageId: string;
  expiresAt: string; state: "available" | "reserved" | "committed" | "consumed" | "cancelled";
}
export interface PendingApproval {
  kind: "checkout_review" | "browser_program" | "browser_operation";
  approvalId: string; actionDigest: string; reason: string; expiresAt: string;
  totalPriceMinor?: number; cartReceipt?: string; commerceRevision?: number;
  program?: string; fallbackCurrentPage?: boolean;
  operation?: BrowserOperation; pageUrl?: string; pageBinding?: string;
}
export interface CartLine { productId: string; variantId: string; quantity: 1; unitPriceMinor: number }

export interface ConversationContext {
  id: string; stateVersion: number; controlOwner: ControlOwner; runState: RunState;
  sessionLifecycle: SessionLifecycle; messages: Message[]; events: ConversationEvent[];
  grants: IntentGrant[]; cart: CartLine[]; commerceRevision: number; observationId: string;
  pendingApproval?: PendingApproval; controlEpoch?: string; lastActivityAt: number;
  agent?: Agent; smolvm?: SmolVMClient; computer?: ComputerSessionClient;
  playwright?: Browser; page?: Page; abortController?: AbortController;
  storefront?: StorefrontController; receipts: Map<string, string>;
  lastBrowserError?: string;
}
