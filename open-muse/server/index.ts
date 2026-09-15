import { createReadStream } from "node:fs";
import { access, stat } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { randomBytes } from "node:crypto";
import httpProxy from "http-proxy";
import { z } from "zod";
import { ConversationManager } from "./manager.js";
import { ModelAccessService, type ModelSelection } from "./model-access.js";
import { parseTraceCursor, type TraceEvent } from "./trace.js";

const messageBody = z.object({ text: z.string().trim().min(1).max(8_000) });
const approvalBody = z.object({ actionDigest: z.string().length(64), approved: z.boolean() });
const resumeBody = z.object({ controlEpoch: z.string().min(12).max(200) });
const authAttemptBody = z.object({ providerId: z.string().min(1).max(80), method: z.enum(["oauth", "api_key"]) }).strict();
const authPromptBody = z.object({ value: z.string().min(1).max(16_384) }).strict();
const selectionBody = z.object({ providerId: z.string().min(1).max(80), modelId: z.string().min(1).max(200) }).strict();
const createConversationBody = z.union([z.object({}).strict(), selectionBody]);
interface LocalSession { csrfToken: string; createdAt: number; lastSeenAt: number; selection?: ModelSelection }
const sessions = new Map<string, LocalSession>();
const SESSION_IDLE_MS = 12 * 60 * 60_000;
const SESSION_ABSOLUTE_MS = 24 * 60 * 60_000;
const MAX_SESSIONS = 32;
const proxy = httpProxy.createProxyServer({ ws: true, xfwd: false, changeOrigin: false });
proxy.on("error", (_error, _request, response) => {
  if (response && "writeHead" in response) { response.writeHead(502); response.end("Live browser proxy unavailable"); }
});

const securityHeaders = {
  "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws://127.0.0.1:*; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'",
  "x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "cache-control": "no-store",
};

export function createApp(manager: ConversationManager, staticRoot = fileURLToPath(new URL("../client", import.meta.url)), modelAccess?: ModelAccessService) {
  const server = createServer(async (request, response) => {
    for (const [name, value] of Object.entries(securityHeaders)) response.setHeader(name, value);
    try { await route(manager, staticRoot, request, response, modelAccess); }
    catch (error) {
      if (response.headersSent) return response.end();
      const status = error instanceof z.ZodError ? 400 : typeof (error as { status?: unknown })?.status === "number" ? (error as { status: number }).status : 500;
      const message = error instanceof z.ZodError ? "Request body is invalid. Check the fields and try again." : status < 500 && error instanceof Error ? error.message : "OpenMuse hit an unexpected error. Check the server log and try again.";
      if (status >= 500) console.error("OpenMuse request failed.");
      sendJson(response, status, { error: message, code: error instanceof z.ZodError ? "invalid_request" : typeof (error as { code?: unknown })?.code === "string" ? (error as { code: string }).code : "request_failed" });
    }
  });
  server.on("upgrade", (request, socket, head) => {
    try {
      const url = new URL(request.url ?? "/", "http://127.0.0.1");
      const match = url.pathname.match(/^\/api\/conversations\/([^/]+)\/viewer\/websockify$/);
      if (!match || !authenticated(request) || !manager.consumeViewerNonce(match[1], url.searchParams.get("token") ?? "")) return socket.destroy();
      request.url = "/websockify";
      proxy.ws(request, socket, head, { target: manager.viewerTarget(match[1]) });
    } catch { socket.destroy(); }
  });
  const cleanupTimer = setInterval(() => cleanupSessions(modelAccess), 15 * 60_000);
  cleanupTimer.unref();
  server.on("close", () => { clearInterval(cleanupTimer); modelAccess?.close(); });
  return server;
}

async function route(manager: ConversationManager, staticRoot: string, request: IncomingMessage, response: ServerResponse, modelAccess?: ModelAccessService): Promise<void> {
  const method = request.method ?? "GET";
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  if (method === "GET" && url.pathname === "/api/bootstrap") {
    assertLoopbackRequest(request);
    cleanupSessions(modelAccess);
    let capability = cookieValue(request);
    let session = capability ? validSession(capability) : undefined;
    if (!session) {
      capability = randomBytes(32).toString("base64url");
      const now = Date.now();
      session = { csrfToken: randomBytes(32).toString("base64url"), createdAt: now, lastSeenAt: now };
      sessions.set(capability, session);
      enforceSessionLimit(modelAccess);
      response.setHeader("set-cookie", `open_muse_session=${capability}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400`);
    } else session.lastSeenAt = Date.now();
    return sendJson(response, 200, {
      csrfToken: session.csrfToken,
      conversationId: manager.activeConversationId,
      ...(modelAccess ? { modelAccess: await modelAccess.snapshot(session.selection) } : {}),
    });
  }
  if (method === "GET" && url.pathname === "/api/health") return sendJson(response, 200, { ready: true });
  if (url.pathname.startsWith("/api/") && !authenticated(request)) throw Object.assign(new Error("Reload OpenMuse to restore the local session."), { status: 401 });

  if (!["GET", "HEAD", "OPTIONS"].includes(method)) assertMutation(request);
  const sessionId = cookieValue(request)!;
  const session = sessions.get(sessionId)!;
  if (modelAccess && method === "GET" && url.pathname === "/api/model-access") return sendJson(response, 200, await modelAccess.snapshot(session.selection));
  if (modelAccess && method === "PUT" && url.pathname === "/api/model-access/selection") {
    const selection = selectionBody.parse(await readJson(request));
    await modelAccess.validateSelection(selection);
    session.selection = selection;
    return sendJson(response, 200, selection);
  }
  if (modelAccess && method === "POST" && url.pathname === "/api/auth-attempts") {
    const body = authAttemptBody.parse(await readJson(request));
    return sendJson(response, 201, { attempt: modelAccess.startAttempt(sessionId, body.providerId, body.method) });
  }
  const attemptMatch = url.pathname.match(/^\/api\/auth-attempts\/([^/]+)$/);
  if (modelAccess && method === "GET" && attemptMatch) return sendJson(response, 200, { attempt: modelAccess.getAttempt(sessionId, attemptMatch[1]) });
  if (modelAccess && method === "DELETE" && attemptMatch) return sendJson(response, 202, { stopping: true, attempt: modelAccess.cancelAttempt(sessionId, attemptMatch[1]) });
  const attemptEventsMatch = url.pathname.match(/^\/api\/auth-attempts\/([^/]+)\/events$/);
  if (modelAccess && method === "GET" && attemptEventsMatch) return streamAuthEvents(modelAccess, sessionId, attemptEventsMatch[1], request, response);
  const promptMatch = url.pathname.match(/^\/api\/auth-attempts\/([^/]+)\/prompts\/([^/]+)$/);
  if (modelAccess && method === "POST" && promptMatch) {
    const body = authPromptBody.parse(await readJson(request));
    return sendJson(response, 200, { attempt: modelAccess.submitPrompt(sessionId, promptMatch[1], promptMatch[2], body.value) });
  }
  const logoutMatch = url.pathname.match(/^\/api\/model-access\/providers\/([^/]+)$/);
  if (modelAccess && method === "DELETE" && logoutMatch) {
    await manager.disconnectProvider(logoutMatch[1]);
    return sendJson(response, 200, { disconnected: true });
  }
  if (method === "GET" && url.pathname === "/api/conversations") return sendJson(response, 200, manager.list());
  if (method === "POST" && url.pathname === "/api/conversations") {
    const body = createConversationBody.parse(await readJson(request));
    const requested = "providerId" in body ? selectionBody.parse(body) : modelAccess ? session.selection : undefined;
    const conversation = await manager.create(requested);
    if (modelAccess) session.selection = { providerId: conversation.providerId, modelId: conversation.modelId };
    return sendJson(response, 201, conversation);
  }
  const tracesMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/traces$/);
  if (method === "GET" && tracesMatch) { assertLoopbackRequest(request); return sendJson(response, 200, manager.traceSnapshot(tracesMatch[1])); }
  const traceEventsMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/traces\/events$/);
  if (method === "GET" && traceEventsMatch) { assertLoopbackRequest(request); return streamTraceEvents(manager, traceEventsMatch[1], request, response, url.searchParams.get("after") ?? undefined); }
  const snapshotMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)$/);
  if (method === "GET" && snapshotMatch) return sendJson(response, 200, manager.snapshot(snapshotMatch[1]));
  const activateMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/activate$/);
  if (method === "POST" && activateMatch) {
    const conversation = await manager.activate(activateMatch[1]);
    if (modelAccess) session.selection = { providerId: conversation.providerId, modelId: conversation.modelId };
    return sendJson(response, 200, conversation);
  }
  const diagnosticsMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/diagnostics$/);
  if (method === "GET" && diagnosticsMatch) {
    assertLoopbackRequest(request);
    return sendJson(response, 200, manager.diagnostics(diagnosticsMatch[1]));
  }
  const messagesMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/messages$/);
  if (method === "POST" && messagesMatch) return sendJson(response, 202, await manager.send(messagesMatch[1], messageBody.parse(await readJson(request)).text));
  const reconnectMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/model-access\/reconnect$/);
  if (modelAccess && method === "POST" && reconnectMatch) return sendJson(response, 200, await manager.reconnectProvider(reconnectMatch[1]));
  const switchModelMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/model-access$/);
  if (modelAccess && method === "PUT" && switchModelMatch) return sendJson(response, 200, await manager.switchModel(switchModelMatch[1], selectionBody.parse(await readJson(request))));
  const eventsMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/events$/);
  if (method === "GET" && eventsMatch) return streamEvents(manager, eventsMatch[1], request, response);
  const approvalMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/approvals\/([^/]+)$/);
  if (method === "POST" && approvalMatch) {
    const body = approvalBody.parse(await readJson(request));
    return sendJson(response, 200, await manager.approve(approvalMatch[1], approvalMatch[2], body.actionDigest, body.approved));
  }
  const takeoverMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/takeover$/);
  if (method === "POST" && takeoverMatch) return sendJson(response, 200, await manager.takeover(takeoverMatch[1]));
  const adoptPopupMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/tabs\/([^/]+)\/adopt$/);
  if (method === "POST" && adoptPopupMatch) return sendJson(response, 200, await manager.adoptPopup(adoptPopupMatch[1], adoptPopupMatch[2]));
  const resumeMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/resume$/);
  if (method === "POST" && resumeMatch) return sendJson(response, 200, await manager.resume(resumeMatch[1], resumeBody.parse(await readJson(request)).controlEpoch));
  const continueMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/continue$/);
  if (method === "POST" && continueMatch) return sendJson(response, 202, await manager.continueInterrupted(continueMatch[1]));
  const startOverMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/start-over$/);
  if (method === "POST" && startOverMatch) return sendJson(response, 201, await manager.startOver(startOverMatch[1]));
  const stopMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/stop$/);
  if (method === "POST" && stopMatch) {
    manager.snapshot(stopMatch[1]);
    void manager.stop(stopMatch[1]).catch(() => console.error("OpenMuse stop failed."));
    return sendJson(response, 202, { stopping: true });
  }
  const tokenMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/viewer-token$/);
  if (method === "POST" && tokenMatch) return sendJson(response, 200, manager.issueViewerNonce(tokenMatch[1]));
  const viewerMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/viewer\/(.+)$/);
  if (method === "GET" && viewerMatch) {
    const target = manager.viewerTarget(viewerMatch[1]);
    if (viewerMatch[2] === "package.json") return sendJson(response, 200, { name: "smolvm-novnc", version: "embedded" });
    const remainder = `/${viewerMatch[2]}${url.search}`;
    request.url = remainder;
    proxy.web(request, response, { target });
    return;
  }
  if (url.pathname.startsWith("/api/")) throw Object.assign(new Error("That OpenMuse route does not exist."), { status: 404 });
  if (method !== "GET" && method !== "HEAD") throw Object.assign(new Error("Method not allowed."), { status: 405 });
  await serveStatic(staticRoot, url.pathname, response, method === "HEAD");
}

function assertLoopbackRequest(request: IncomingMessage): void {
  const host = (request.headers.host ?? "").split(":")[0].replace(/^\[|\]$/g, "");
  if (!new Set(["127.0.0.1", "localhost", "::1"]).has(host)) throw Object.assign(new Error("OpenMuse only accepts requests from this computer."), { status: 403 });
  const site = request.headers["sec-fetch-site"];
  if (site && site !== "same-origin" && site !== "none") throw Object.assign(new Error("Open the local OpenMuse page directly on this computer."), { status: 403 });
}

function cookieValue(request: IncomingMessage): string | undefined {
  return request.headers.cookie?.split(";").map((part) => part.trim()).find((part) => part.startsWith("open_muse_session="))?.slice("open_muse_session=".length);
}
function validSession(capability: string): LocalSession | undefined {
  const session = sessions.get(capability);
  if (!session) return;
  const now = Date.now();
  if (now - session.lastSeenAt > SESSION_IDLE_MS || now - session.createdAt > SESSION_ABSOLUTE_MS) return;
  return session;
}
function authenticated(request: IncomingMessage): boolean {
  const value = cookieValue(request);
  const session = value ? validSession(value) : undefined;
  if (session) session.lastSeenAt = Date.now();
  return Boolean(session);
}
function cleanupSessions(modelAccess?: ModelAccessService): void {
  const now = Date.now();
  for (const [capability, session] of sessions) {
    if (now - session.lastSeenAt > SESSION_IDLE_MS || now - session.createdAt > SESSION_ABSOLUTE_MS) {
      modelAccess?.cancelSessionAttempts(capability);
      sessions.delete(capability);
    }
  }
  enforceSessionLimit(modelAccess);
}
function enforceSessionLimit(modelAccess?: ModelAccessService): void {
  while (sessions.size > MAX_SESSIONS) {
    const oldest = [...sessions.entries()].sort((a, b) => a[1].lastSeenAt - b[1].lastSeenAt)[0];
    if (!oldest) return;
    modelAccess?.cancelSessionAttempts(oldest[0]);
    sessions.delete(oldest[0]);
  }
}
function assertMutation(request: IncomingMessage): void {
  assertLoopbackRequest(request);
  const origin = request.headers.origin;
  if (!origin || !["127.0.0.1", "localhost", "::1"].includes(new URL(origin).hostname)) throw Object.assign(new Error("Reload OpenMuse and try again."), { status: 403 });
  const capability = cookieValue(request)!;
  if (request.headers["x-smol-csrf"] !== sessions.get(capability)?.csrfToken) throw Object.assign(new Error("Reload OpenMuse and try again."), { status: 403 });
  if (!(request.headers["content-type"] ?? "").startsWith("application/json")) throw Object.assign(new Error("Requests must use JSON."), { status: 415 });
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = []; let size = 0;
  for await (const chunk of request) { const bytes = Buffer.from(chunk); size += bytes.length; if (size > 32 * 1024) throw Object.assign(new Error("Request body is too large."), { status: 413 }); chunks.push(bytes); }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"); } catch { throw Object.assign(new Error("Request body must be valid JSON."), { status: 400 }); }
}

function streamAuthEvents(modelAccess: ModelAccessService, sessionId: string, attemptId: string, request: IncomingMessage, response: ServerResponse): void {
  modelAccess.getAttempt(sessionId, attemptId);
  response.statusCode = 200; response.setHeader("content-type", "text/event-stream"); response.setHeader("connection", "keep-alive"); response.setHeader("x-accel-buffering", "no"); response.flushHeaders();
  const afterId = Number(request.headers["last-event-id"] ?? 0) || 0;
  let unsubscribe: () => void = () => undefined;
  unsubscribe = modelAccess.subscribe(sessionId, attemptId, (event) => {
    response.write(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
    if (["auth.succeeded", "auth.failed", "auth.cancelled", "auth.expired", "auth.resync_required"].includes(event.type)) queueMicrotask(() => { unsubscribe(); response.end(); });
  }, afterId);
  const heartbeat = setInterval(() => response.write(": heartbeat\n\n"), 15_000);
  request.on("close", () => { clearInterval(heartbeat); unsubscribe(); });
}

function streamEvents(manager: ConversationManager, id: string, request: IncomingMessage, response: ServerResponse): void {
  response.statusCode = 200; response.setHeader("content-type", "text/event-stream"); response.setHeader("connection", "keep-alive"); response.setHeader("x-accel-buffering", "no"); response.flushHeaders();
  const afterId = Number(request.headers["last-event-id"] ?? 0) || 0;
  const unsubscribe = manager.subscribe(id, (event) => response.write(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`), afterId);
  if (!unsubscribe) { response.end(`event: error\ndata: {"error":"Conversation not found"}\n\n`); return; }
  const heartbeat = setInterval(() => response.write(": heartbeat\n\n"), 15_000);
  request.on("close", () => { clearInterval(heartbeat); unsubscribe(); });
}

function streamTraceEvents(manager: ConversationManager, id: string, request: IncomingMessage, response: ServerResponse, after?: string): void {
  response.statusCode = 200;
  response.setHeader("content-type", "text/event-stream");
  response.setHeader("connection", "keep-alive");
  response.setHeader("x-accel-buffering", "no");
  response.setHeader("cache-control", "no-store");
  response.flushHeaders();
  const cursor = parseTraceCursor(typeof request.headers["last-event-id"] === "string" ? request.headers["last-event-id"] : after);
  let closed = false;
  let unsubscribe: () => void = () => undefined;
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  const close = () => {
    if (closed) return;
    closed = true;
    if (heartbeat) clearInterval(heartbeat);
    unsubscribe();
    response.end();
  };
  const closeAfterDrain = () => {
    if (closed) return;
    closed = true;
    if (heartbeat) clearInterval(heartbeat);
    unsubscribe();
    response.once("drain", () => response.end());
  };
  const write = (event: TraceEvent): void => {
    if (closed) return;
    const ok = response.write(`id: ${event.streamId}:${event.eventId}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
    if (!ok) closeAfterDrain();
    else if (event.type === "trace.resync_required") queueMicrotask(close);
  };
  const subscription = manager.subscribeTraces(id, write, cursor);
  unsubscribe = subscription.unsubscribe ?? (() => undefined);
  if (subscription.resync) {
    const snapshot = manager.traceSnapshot(id);
    response.end(`id: ${snapshot.streamId}:${snapshot.cursor}\nevent: trace.resync_required\ndata: ${JSON.stringify({ type: "trace.resync_required", streamId: snapshot.streamId, eventId: snapshot.cursor, conversationId: id, createdAt: new Date().toISOString() })}\n\n`);
    return;
  }
  if (closed) { unsubscribe(); return; }
  heartbeat = setInterval(() => { if (!response.write(": heartbeat\n\n")) closeAfterDrain(); }, 15_000);
  request.on("close", close);
}

async function serveStatic(root: string, pathname: string, response: ServerResponse, head: boolean): Promise<void> {
  const relative = normalize(decodeURIComponent(pathname)).replace(/^([/\\])+/, ""); let target = join(root, relative || "index.html");
  if (target !== root && !target.startsWith(`${root}${sep}`)) throw Object.assign(new Error("Not found."), { status: 404 });
  try { const info = await stat(target); if (info.isDirectory()) target = join(target, "index.html"); await access(target); }
  catch { if (extname(target)) throw Object.assign(new Error("Not found."), { status: 404 }); target = join(root, "index.html"); }
  const mime: Record<string, string> = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml" };
  response.statusCode = 200; response.setHeader("content-type", mime[extname(target)] ?? "application/octet-stream");
  if (head) { response.end(); return; } createReadStream(target).pipe(response);
}
function sendJson(response: ServerResponse, status: number, body: unknown): void { response.statusCode = status; response.setHeader("content-type", "application/json; charset=utf-8"); response.end(JSON.stringify(body)); }

export function startupFailureMessage(error: unknown): string {
  const detail = error instanceof Error && error.message.trim() ? error.message.trim() : "An unknown error occurred.";
  return `OpenMuse could not start: ${detail}`;
}

async function main(): Promise<void> {
  try { process.loadEnvFile(".env.local"); } catch { /* optional */ }
  const host = process.env.OPEN_MUSE_HOST ?? "127.0.0.1"; const port = Number(process.env.OPEN_MUSE_PORT ?? 4318);
  if (host !== "127.0.0.1") throw new Error("OpenMuse only listens locally. Set OPEN_MUSE_HOST=127.0.0.1.");
  const modelAccess = ModelAccessService.createDefault();
  const manager = await ConversationManager.open(
    process.env.OPENAI_API_KEY ?? "",
    process.env.OPENAI_MODEL ?? "gpt-5.6-luna",
    process.env.OPEN_MUSE_FIXTURE_STORE === "1",
    undefined,
    {},
    modelAccess,
  );
  const server = createApp(manager, fileURLToPath(new URL("../client", import.meta.url)), modelAccess); let closing = false;
  const shutdown = async () => { if (closing) return; closing = true; modelAccess.close(); server.close(); await manager.close(); };
  process.once("SIGINT", () => void shutdown()); process.once("SIGTERM", () => void shutdown());
  server.listen(port, host, () => console.log(`OpenMuse is ready at http://${host}:${port}`));
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  void main().catch((error: unknown) => {
    console.error(startupFailureMessage(error));
    process.exitCode = 1;
  });
}

export const _test = { assertLoopbackRequest };
