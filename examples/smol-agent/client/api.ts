export interface Message { id: string; role: "user" | "assistant"; text: string; createdAt: string }
export interface Approval { approvalId: string; actionDigest: string; reason: string; expiresAt: string; totalPriceMinor: number }
export interface Event { id: number; type: string; createdAt: string; payload: Record<string, unknown> }
export interface Conversation {
  id: string; stateVersion: number; controlOwner: "agent" | "pause_requested" | "human";
  runState: string; sessionLifecycle: string; messages: Message[]; pendingApproval?: Approval;
  viewerReady: boolean; events: Event[];
}

let csrfToken = "";
export async function bootstrap(): Promise<void> {
  const response = await fetch("/api/bootstrap", { credentials: "same-origin" });
  if (!response.ok) throw new Error("Could not start the local Smol Agent session.");
  csrfToken = (await response.json() as { csrfToken: string }).csrfToken;
}
async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method, credentials: "same-origin",
    headers: method === "GET" ? undefined : { "content-type": "application/json", "x-smol-csrf": csrfToken },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({})) as T & { error?: string };
  if (!response.ok) throw new Error(data.error ?? "Smol Agent request failed.");
  return data;
}
export const createConversation = () => request<Conversation>("/api/conversations", "POST", {});
export const getConversation = (id: string) => request<Conversation>(`/api/conversations/${id}`);
export const sendMessage = (id: string, text: string) => request(`/api/conversations/${id}/messages`, "POST", { text });
export const stopConversation = (id: string) => request(`/api/conversations/${id}/stop`, "POST", {});
export const takeOver = (id: string) => request<{ controlEpoch: string }>(`/api/conversations/${id}/takeover`, "POST", {});
export const resume = (id: string, controlEpoch: string) => request<Conversation>(`/api/conversations/${id}/resume`, "POST", { controlEpoch });
export const resolveApproval = (id: string, approval: Approval, approved: boolean) => request<Conversation>(`/api/conversations/${id}/approvals/${approval.approvalId}`, "POST", { actionDigest: approval.actionDigest, approved });
export const viewerToken = (id: string) => request<{ viewerPath: string }>(`/api/conversations/${id}/viewer-token`, "POST", {});
