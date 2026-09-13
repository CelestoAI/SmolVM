import { createReadStream } from "node:fs";
import { access, stat } from "node:fs/promises";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { z } from "zod";
import { createWorkflow } from "./agent.js";
import { validateArtifactName } from "./artifact-contract.js";
import { readConfig } from "./config.js";
import { PublicError, redactedLog, toPublicError } from "./errors.js";
import { RunManager } from "./run-manager.js";

const planBody = z.object({
  goal: z.string().trim().min(10).max(1000),
  constraints: z.array(z.string().trim().min(1).max(300)).max(20).default([]),
});
const runBody = z.object({ planId: z.string().uuid() });
const constraintBody = z.object({ constraint: z.string().trim().min(1).max(300) });

const securityHeaders = {
  "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
  "x-content-type-options": "nosniff",
  "referrer-policy": "no-referrer",
  "cache-control": "no-store",
};

export function createApp(manager: RunManager, staticRoot = fileURLToPath(new URL("../client", import.meta.url))) {
  return createServer(async (request, response) => {
    for (const [name, value] of Object.entries(securityHeaders)) response.setHeader(name, value);
    try {
      await route(manager, staticRoot, request, response);
    } catch (error) {
      if (response.headersSent) {
        response.end();
        return;
      }
      const publicError = toHttpError(error);
      sendJson(response, publicError.status, { error: publicError.message, recovery: publicError.recovery });
      if (publicError.status >= 500) console.error(redactedLog(error));
    }
  });
}

async function route(manager: RunManager, staticRoot: string, request: IncomingMessage, response: ServerResponse): Promise<void> {
  const method = request.method ?? "GET";
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  if (method === "GET" && url.pathname === "/api/health") return sendJson(response, 200, { ready: true });
  if (method === "POST" && url.pathname === "/api/plans") {
    assertMutationRequest(request, true);
    const body = parseRequest(planBody, await readJson(request));
    const plan = await manager.createPlan(body.goal, body.constraints);
    return sendJson(response, 201, { planId: plan.id, goal: plan.goal, constraints: plan.constraints, steps: plan.steps });
  }
  if (method === "POST" && url.pathname === "/api/runs") {
    assertMutationRequest(request, true);
    const body = parseRequest(runBody, await readJson(request));
    return sendJson(response, 202, manager.start(body.planId));
  }

  const runMatch = url.pathname.match(/^\/api\/runs\/([^/]+)$/);
  if (method === "GET" && runMatch) {
    const run = manager.get(runMatch[1]);
    if (!run) throw Object.assign(new Error("That Open Muse run was not found."), { status: 404 });
    return sendJson(response, 200, run);
  }
  const eventsMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/events$/);
  if (method === "GET" && eventsMatch) return streamEvents(manager, eventsMatch[1], request, response);
  const constraintMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/constraints$/);
  if (method === "POST" && constraintMatch) {
    assertMutationRequest(request, true);
    const body = parseRequest(constraintBody, await readJson(request));
    return sendJson(response, 200, manager.addConstraint(constraintMatch[1], body.constraint));
  }
  const cancelMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/cancel$/);
  if (method === "POST" && cancelMatch) {
    assertMutationRequest(request, true);
    return sendJson(response, 202, manager.cancel(cancelMatch[1]));
  }
  const artifactMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/artifacts\/([^/]+)$/);
  if (method === "GET" && artifactMatch) {
    const name = validateArtifactName(decodeURIComponent(artifactMatch[2]));
    const bytes = manager.artifact(artifactMatch[1], name);
    if (!bytes) throw Object.assign(new Error("That artifact is not ready."), { status: 404 });
    response.statusCode = 200;
    response.setHeader("content-type", name.endsWith(".md") ? "text/markdown; charset=utf-8" : name.endsWith(".json") ? "application/json" : "text/csv; charset=utf-8");
    response.setHeader("content-disposition", `attachment; filename="${name}"`);
    response.end(Buffer.from(bytes));
    return;
  }
  const packetMatch = url.pathname.match(/^\/api\/runs\/([^/]+)\/packet\.zip$/);
  if (method === "GET" && packetMatch) {
    const bytes = manager.packet(packetMatch[1]);
    if (!bytes) throw Object.assign(new Error("The research packet is not ready."), { status: 404 });
    response.statusCode = 200;
    response.setHeader("content-type", "application/zip");
    response.setHeader("content-disposition", "attachment; filename=Open-Muse-packet.zip");
    response.end(Buffer.from(bytes));
    return;
  }
  if (url.pathname.startsWith("/api/")) throw Object.assign(new Error("That Open Muse route does not exist."), { status: 404 });
  if (method !== "GET" && method !== "HEAD") throw Object.assign(new Error("Method not allowed."), { status: 405 });
  await serveStatic(staticRoot, url.pathname, response, method === "HEAD");
}

function parseRequest<T>(schema: z.ZodType<T>, value: unknown): T {
  try {
    return schema.parse(value);
  } catch (error) {
    if (error instanceof z.ZodError) throw new PublicError("Check the goal and constraints and try again.", 400);
    throw error;
  }
}

function assertMutationRequest(request: Pick<IncomingMessage, "headers">, expectsJson: boolean): void {
  if (expectsJson && request.headers["content-type"]?.split(";", 1)[0]?.trim().toLowerCase() !== "application/json") {
    throw Object.assign(new Error("Request body must use application/json."), { status: 415 });
  }
  const origin = request.headers.origin;
  const host = request.headers.host;
  if (origin && (!host || origin !== `http://${host}`)) {
    throw Object.assign(new Error("Cross-site requests are not allowed."), { status: 403 });
  }
  if (request.headers["sec-fetch-site"] === "cross-site") {
    throw Object.assign(new Error("Cross-site requests are not allowed."), { status: 403 });
  }
}

function toHttpError(error: unknown): PublicError {
  if (error instanceof PublicError) return error;
  const status = typeof (error as { status?: unknown })?.status === "number"
    ? (error as { status: number }).status
    : 500;
  return status < 500 && error instanceof Error ? new PublicError(error.message, status) : toPublicError(error);
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const bytes = Buffer.from(chunk);
    size += bytes.byteLength;
    if (size > 64 * 1024) throw Object.assign(new Error("Request body is larger than 64 KiB."), { status: 413 });
    chunks.push(bytes);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"); }
  catch { throw Object.assign(new Error("Request body must be valid JSON."), { status: 400 }); }
}

function streamEvents(manager: RunManager, id: string, request: IncomingMessage, response: ServerResponse): void {
  response.statusCode = 200;
  response.setHeader("content-type", "text/event-stream");
  response.setHeader("connection", "keep-alive");
  response.setHeader("x-accel-buffering", "no");
  response.flushHeaders();
  const afterId = Number(request.headers["last-event-id"] ?? 0) || 0;
  const unsubscribe = manager.subscribe(id, (event) => {
    response.write(`id: ${event.id}\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
  }, afterId);
  if (!unsubscribe) {
    response.end(`event: error\ndata: ${JSON.stringify({ error: "That Open Muse run was not found." })}\n\n`);
    return;
  }
  const heartbeat = setInterval(() => response.write(": heartbeat\n\n"), 15_000);
  request.on("close", () => {
    clearInterval(heartbeat);
    unsubscribe();
  });
}

async function serveStatic(root: string, pathname: string, response: ServerResponse, head: boolean): Promise<void> {
  const relative = normalize(decodeURIComponent(pathname)).replace(/^([/\\])+/, "");
  let target = join(root, relative || "index.html");
  if (target !== root && !target.startsWith(`${root}${sep}`)) throw Object.assign(new Error("Not found."), { status: 404 });
  try {
    const info = await stat(target);
    if (info.isDirectory()) target = join(target, "index.html");
    await access(target);
  } catch {
    if (extname(target)) throw Object.assign(new Error("Not found."), { status: 404 });
    target = join(root, "index.html");
  }
  const mime: Record<string, string> = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml" };
  response.statusCode = 200;
  response.setHeader("content-type", mime[extname(target)] ?? "application/octet-stream");
  if (head) {
    response.end();
    return;
  }
  createReadStream(target).pipe(response);
}

function sendJson(response: ServerResponse, status: number, body: unknown): void {
  response.statusCode = status;
  response.setHeader("content-type", "application/json; charset=utf-8");
  response.end(JSON.stringify(body));
}

async function main(): Promise<void> {
  try { process.loadEnvFile(".env.local"); } catch { /* .env.local is optional. */ }
  const config = readConfig();
  const manager = new RunManager(createWorkflow(config.apiKey, config.model));
  const server = createApp(manager);
  let closing = false;
  const shutdown = async (error?: unknown) => {
    if (closing) return;
    closing = true;
    if (error) console.error(redactedLog(error));
    server.close();
    await manager.close();
    process.exitCode = error ? 1 : 0;
  };
  process.once("SIGINT", () => void shutdown());
  process.once("SIGTERM", () => void shutdown());
  process.once("uncaughtException", (error) => void shutdown(error));
  process.once("unhandledRejection", (error) => void shutdown(error));
  server.listen(config.port, config.host, () => console.log(`Open Muse is ready at http://${config.host}:${config.port}`));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) void main();

export const _test = { assertMutationRequest, parseRequest, toHttpError };
