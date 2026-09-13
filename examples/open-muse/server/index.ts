import { createReadStream } from "node:fs";
import { access, stat } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { randomBytes } from "node:crypto";
import httpProxy from "http-proxy";
import { z } from "zod";
import { ConversationManager } from "./manager.js";

const messageBody = z.object({ text: z.string().trim().min(1).max(8_000) });
const approvalBody = z.object({ actionDigest: z.string().length(64), approved: z.boolean() });
const resumeBody = z.object({ controlEpoch: z.string().min(12).max(200) });
const sessions = new Map<string, string>();
const proxy = httpProxy.createProxyServer({ ws: true, xfwd: false, changeOrigin: false });
proxy.on("error", (_error, _request, response) => {
  if (response && "writeHead" in response) { response.writeHead(502); response.end("Live browser proxy unavailable"); }
});

const securityHeaders = {
  "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self' ws://127.0.0.1:*; frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'none'",
  "x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "cache-control": "no-store",
};

export function createApp(manager: ConversationManager, staticRoot = fileURLToPath(new URL("../client", import.meta.url))) {
  const server = createServer(async (request, response) => {
    for (const [name, value] of Object.entries(securityHeaders)) response.setHeader(name, value);
    try { await route(manager, staticRoot, request, response); }
    catch (error) {
      if (response.headersSent) return response.end();
      const status = typeof (error as { status?: unknown })?.status === "number" ? (error as { status: number }).status : 500;
      const message = status < 500 && error instanceof Error ? error.message : "OpenMuse hit an unexpected error. Check the server log and try again.";
      if (status >= 500) console.error(error instanceof Error ? error.message : error);
      sendJson(response, status, { error: message });
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
  return server;
}

async function route(manager: ConversationManager, staticRoot: string, request: IncomingMessage, response: ServerResponse): Promise<void> {
  const method = request.method ?? "GET";
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  if (method === "GET" && url.pathname === "/api/bootstrap") {
    assertLoopbackRequest(request);
    const capability = randomBytes(32).toString("base64url");
    const csrfToken = randomBytes(32).toString("base64url");
    sessions.set(capability, csrfToken);
    response.setHeader("set-cookie", `open_muse_session=${capability}; HttpOnly; SameSite=Strict; Path=/`);
    return sendJson(response, 200, { csrfToken, conversationId: manager.activeConversationId });
  }
  if (method === "GET" && url.pathname === "/api/health") return sendJson(response, 200, { ready: true });
  if (url.pathname.startsWith("/api/") && !authenticated(request)) throw Object.assign(new Error("Reload OpenMuse to restore the local session."), { status: 401 });

  if (method === "POST") assertMutation(request);
  if (method === "POST" && url.pathname === "/api/conversations") return sendJson(response, 201, await manager.create());
  const snapshotMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)$/);
  if (method === "GET" && snapshotMatch) return sendJson(response, 200, manager.snapshot(snapshotMatch[1]));
  const messagesMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/messages$/);
  if (method === "POST" && messagesMatch) return sendJson(response, 202, manager.send(messagesMatch[1], messageBody.parse(await readJson(request)).text));
  const eventsMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/events$/);
  if (method === "GET" && eventsMatch) return streamEvents(manager, eventsMatch[1], request, response);
  const approvalMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/approvals\/([^/]+)$/);
  if (method === "POST" && approvalMatch) {
    const body = approvalBody.parse(await readJson(request));
    return sendJson(response, 200, await manager.approve(approvalMatch[1], approvalMatch[2], body.actionDigest, body.approved));
  }
  const takeoverMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/takeover$/);
  if (method === "POST" && takeoverMatch) return sendJson(response, 200, await manager.takeover(takeoverMatch[1]));
  const resumeMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/resume$/);
  if (method === "POST" && resumeMatch) return sendJson(response, 200, manager.resume(resumeMatch[1], resumeBody.parse(await readJson(request)).controlEpoch));
  const stopMatch = url.pathname.match(/^\/api\/conversations\/([^/]+)\/stop$/);
  if (method === "POST" && stopMatch) {
    manager.snapshot(stopMatch[1]);
    void manager.stop(stopMatch[1]).catch((error) => console.error(error instanceof Error ? error.message : error));
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
function authenticated(request: IncomingMessage): boolean { const value = cookieValue(request); return Boolean(value && sessions.has(value)); }
function assertMutation(request: IncomingMessage): void {
  assertLoopbackRequest(request);
  const origin = request.headers.origin;
  if (!origin || !["127.0.0.1", "localhost", "::1"].includes(new URL(origin).hostname)) throw Object.assign(new Error("Reload OpenMuse and try again."), { status: 403 });
  const capability = cookieValue(request)!;
  if (request.headers["x-smol-csrf"] !== sessions.get(capability)) throw Object.assign(new Error("Reload OpenMuse and try again."), { status: 403 });
  if (!(request.headers["content-type"] ?? "").startsWith("application/json")) throw Object.assign(new Error("Requests must use JSON."), { status: 415 });
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = []; let size = 0;
  for await (const chunk of request) { const bytes = Buffer.from(chunk); size += bytes.length; if (size > 64 * 1024) throw Object.assign(new Error("Request body is too large."), { status: 413 }); chunks.push(bytes); }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"); } catch { throw Object.assign(new Error("Request body must be valid JSON."), { status: 400 }); }
}

function streamEvents(manager: ConversationManager, id: string, request: IncomingMessage, response: ServerResponse): void {
  response.statusCode = 200; response.setHeader("content-type", "text/event-stream"); response.setHeader("connection", "keep-alive"); response.setHeader("x-accel-buffering", "no"); response.flushHeaders();
  const afterId = Number(request.headers["last-event-id"] ?? 0) || 0;
  const unsubscribe = manager.subscribe(id, (event) => response.write(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`), afterId);
  if (!unsubscribe) { response.end(`event: error\ndata: {"error":"Conversation not found"}\n\n`); return; }
  const heartbeat = setInterval(() => response.write(": heartbeat\n\n"), 15_000);
  request.on("close", () => { clearInterval(heartbeat); unsubscribe(); });
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

async function main(): Promise<void> {
  try { process.loadEnvFile(".env.local"); } catch { /* optional */ }
  const host = process.env.OPEN_MUSE_HOST ?? "127.0.0.1"; const port = Number(process.env.OPEN_MUSE_PORT ?? 4318);
  if (host !== "127.0.0.1") throw new Error("OpenMuse only listens locally. Set OPEN_MUSE_HOST=127.0.0.1.");
  const manager = new ConversationManager(
    process.env.OPENAI_API_KEY ?? "",
    process.env.OPENAI_MODEL ?? "gpt-5-mini",
    process.env.OPEN_MUSE_FIXTURE_STORE === "1",
  );
  const server = createApp(manager); let closing = false;
  const shutdown = async () => { if (closing) return; closing = true; server.close(); await manager.close(); };
  process.once("SIGINT", () => void shutdown()); process.once("SIGTERM", () => void shutdown());
  server.listen(port, host, () => console.log(`OpenMuse is ready at http://${host}:${port}`));
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) void main();

export const _test = { assertLoopbackRequest };
