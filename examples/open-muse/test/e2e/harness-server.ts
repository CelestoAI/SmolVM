import { createServer, type ServerResponse } from "node:http";

interface HarnessConversation {
  id: string;
  stateVersion: number;
  controlOwner: "agent" | "human";
  runState: string;
  sessionLifecycle: "idle" | "deleted";
  viewerReady: boolean;
  messages: Array<{ id: string; role: "user" | "assistant"; text: string; createdAt: string }>;
  events: Array<{ id: number; type: string; createdAt: string; payload: Record<string, unknown> }>;
  pendingApproval?: {
    kind: "browser_operation";
    approvalId: string;
    actionDigest: string;
    reason: string;
    expiresAt: string;
    operation: { kind: "navigate"; url: string };
  };
  recovery?: {
    kind: "failed_before_execution" | "outcome_unknown" | "interrupted";
    operationId?: string;
    summary?: string;
  };
}

type Scenario = "success" | "failed_before_execution" | "outcome_unknown";

const subscribers = new Set<ServerResponse>();
let conversation: HarnessConversation | undefined;
let messageId = 0;
let approvalId = 0;
let scenario: Scenario = "success";
let dispatchCount = 0;
let terminalCount = 0;
let observationCount = 0;
let approvalResolution: Promise<void> | undefined;

function freshConversation(): HarnessConversation {
  return {
    id: "conversation-scripted",
    stateVersion: 1,
    controlOwner: "agent",
    runState: "idle",
    sessionLifecycle: "idle",
    viewerReady: true,
    messages: [],
    events: [],
  };
}

function requestApproval(reason = "Open example.com"): void {
  if (!conversation) return;
  conversation.runState = "waiting_for_approval";
  conversation.pendingApproval = {
    kind: "browser_operation",
    approvalId: `approval-scripted-${++approvalId}`,
    actionDigest: "a".repeat(64),
    reason,
    expiresAt: new Date(Date.now() + 300_000).toISOString(),
    operation: { kind: "navigate", url: "https://example.com" },
  };
  publish("approval.requested", { summary: "Navigation approval requested" });
}

function sendJson(response: ServerResponse, status: number, value: unknown): void {
  response.statusCode = status;
  response.setHeader("content-type", "application/json; charset=utf-8");
  response.end(JSON.stringify(value));
}

function publish(type: string, payload: Record<string, unknown>): void {
  if (!conversation) return;
  const event = { id: conversation.events.length + 1, type, createdAt: new Date().toISOString(), payload };
  conversation.events.push(event);
  const frame = `id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
  for (const response of subscribers) response.write(frame);
}

async function body(request: import("node:http").IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown> : {};
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  const method = request.method ?? "GET";
  if (method === "POST" && url.pathname === "/__e2e/reset") {
    for (const subscriber of subscribers) subscriber.end();
    subscribers.clear();
    conversation = undefined;
    messageId = 0;
    approvalId = 0;
    scenario = "success";
    dispatchCount = 0;
    terminalCount = 0;
    observationCount = 0;
    approvalResolution = undefined;
    return sendJson(response, 200, { reset: true });
  }
  if (method === "POST" && url.pathname === "/__e2e/scenario") {
    const input = await body(request);
    if (!new Set(["success", "failed_before_execution", "outcome_unknown"]).has(String(input.scenario))) {
      return sendJson(response, 400, { error: "Unknown scenario." });
    }
    scenario = input.scenario as Scenario;
    return sendJson(response, 200, { scenario });
  }
  if (method === "GET" && url.pathname === "/__e2e/state") {
    return sendJson(response, 200, { scenario, dispatchCount, terminalCount, observationCount, conversation });
  }
  if (method === "GET" && url.pathname === "/__e2e/viewer") {
    response.statusCode = 200;
    response.setHeader("content-type", "text/html; charset=utf-8");
    response.end("<!doctype html><title>Scripted viewer</title><p>Deterministic browser viewer</p>");
    return;
  }
  if (url.pathname === "/api/health") return sendJson(response, 200, { ready: true, deterministic: true });
  if (url.pathname === "/api/bootstrap") {
    response.setHeader("set-cookie", "open_muse_session=e2e; HttpOnly; SameSite=Strict; Path=/");
    return sendJson(response, 200, { csrfToken: "csrf-scripted", conversationId: conversation?.id });
  }
  if (method === "POST" && url.pathname === "/api/conversations") {
    conversation = freshConversation();
    return sendJson(response, 201, conversation);
  }
  const match = url.pathname.match(/^\/api\/conversations\/([^/]+)(?:\/(.*))?$/);
  if (!match || !conversation || match[1] !== conversation.id) return sendJson(response, 404, { error: "Conversation not found." });
  const route = match[2] ?? "";
  if (method === "GET" && route === "") return sendJson(response, 200, conversation);
  if (method === "GET" && route === "events") {
    response.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
    subscribers.add(response);
    request.on("close", () => subscribers.delete(response));
    return;
  }
  if (method === "POST" && route === "viewer-token") {
    return sendJson(response, 200, { viewerPath: "/__e2e/viewer" });
  }
  if (method === "POST" && route === "messages") {
    const input = await body(request);
    conversation.messages.push({ id: `message-${++messageId}`, role: "user", text: String(input.text ?? ""), createdAt: new Date().toISOString() });
    requestApproval();
    return sendJson(response, 202, conversation);
  }
  if (method === "POST" && route.startsWith("approvals/")) {
    const input = await body(request);
    const approved = input.approved === true;
    if (!approvalResolution) {
      approvalResolution = (async () => {
        await new Promise((resolve) => setTimeout(resolve, 25));
        if (!conversation?.pendingApproval) return;
        conversation.pendingApproval = undefined;
        if (!approved) {
          conversation.runState = "idle";
          conversation.messages.push({ id: `message-${++messageId}`, role: "assistant", text: "I did not open the website.", createdAt: new Date().toISOString() });
          terminalCount += 1;
          publish("approval.resolved", { summary: "Navigation declined" });
          publish("message.completed", {});
          return;
        }
        publish("operation.approved", { summary: "Open example.com" });
        if (scenario === "failed_before_execution") {
          terminalCount += 1;
          conversation.runState = "interrupted";
          conversation.recovery = { kind: "failed_before_execution", operationId: "operation-safe", summary: "Open example.com" };
          publish("operation.completed", { summary: "Open example.com" });
          return;
        }
        dispatchCount += 1;
        publish("operation.dispatched", { summary: "Open example.com" });
        if (scenario === "outcome_unknown") {
          terminalCount += 1;
          conversation.runState = "interrupted";
          conversation.recovery = { kind: "outcome_unknown", operationId: "operation-unknown", summary: "Open example.com" };
          publish("operation.outcome_unknown", { summary: "Open example.com" });
          return;
        }
        terminalCount += 1;
        conversation.runState = "idle";
        conversation.messages.push({ id: `message-${++messageId}`, role: "assistant", text: "The scripted browser opened Example Domain.", createdAt: new Date().toISOString() });
        publish("operation.completed", { summary: "Open example.com" });
        publish("approval.resolved", { summary: "Navigation approved" });
        publish("message.completed", {});
      })().finally(() => { approvalResolution = undefined; });
    }
    await approvalResolution;
    return sendJson(response, 200, conversation);
  }
  if (method === "POST" && route === "continue") {
    const recovery = conversation.recovery;
    conversation.recovery = undefined;
    if (recovery?.kind === "failed_before_execution") requestApproval("Retry opening example.com with fresh approval");
    else {
      conversation.runState = "idle";
      observationCount += 1;
      conversation.messages.push({ id: `message-${++messageId}`, role: "assistant", text: "I will inspect the current page before deciding what to do next.", createdAt: new Date().toISOString() });
      publish("message.completed", {});
    }
    return sendJson(response, 202, conversation);
  }
  if (method === "POST" && route === "start-over") {
    conversation = { ...freshConversation(), id: "conversation-replacement" };
    return sendJson(response, 201, conversation);
  }
  if (method === "POST" && route === "stop") {
    conversation.runState = "stopped";
    conversation.sessionLifecycle = "deleted";
    conversation.viewerReady = false;
    publish("conversation.stopped", { summary: "Conversation stopped" });
    return sendJson(response, 200, conversation);
  }
  if (method === "POST" && route === "takeover") {
    conversation.controlOwner = "human";
    publish("control.changed", { summary: "You have control" });
    return sendJson(response, 200, { controlEpoch: "control-scripted" });
  }
  if (method === "POST" && route === "resume") {
    conversation.controlOwner = "agent";
    publish("control.changed", { summary: "Agent has control" });
    return sendJson(response, 200, conversation);
  }
  return sendJson(response, 404, { error: "The deterministic harness does not implement this route." });
});

server.listen(4318, "127.0.0.1", () => console.log("Deterministic OpenMuse harness ready at http://127.0.0.1:4318"));

function close(): void {
  for (const response of subscribers) response.end();
  server.close(() => process.exit(0));
}
process.once("SIGINT", close);
process.once("SIGTERM", close);
