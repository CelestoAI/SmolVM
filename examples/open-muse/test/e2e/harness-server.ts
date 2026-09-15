import { EventEmitter } from "node:events";
import { createServer, type Server } from "node:http";
import { mkdtemp, rm, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { Agent } from "@earendil-works/pi-agent-core";
import type { Browser, BrowserContext, Frame, Page } from "playwright-core";
import type { ActionBroker } from "../../server/broker.js";
import { createApp } from "../../server/index.js";
import { ConversationManager, type RuntimeDependencies } from "../../server/manager.js";
import { ConversationStateStore } from "../../server/state-store.js";
import type { ConversationContext } from "../../server/types.js";

type Scenario = "success" | "failed_before_execution" | "outcome_unknown";

class FakePage extends EventEmitter {
  private closed = false;
  private readonly frame = {} as Frame;
  url(): string { return "https://example.com/"; }
  isClosed(): boolean { return this.closed; }
  mainFrame(): Frame { return this.frame; }
  async title(): Promise<string> { return "Example Domain"; }
  closePage(): void { this.closed = true; this.emit("close"); }
}

class FakeBrowserContext extends EventEmitter {
  readonly page = new FakePage();
  pages(): Page[] { return [this.page as unknown as Page]; }
  async newPage(): Promise<Page> { return this.page as unknown as Page; }
}

class FakeBrowser {
  readonly context = new FakeBrowserContext();
  private connected = true;
  contexts(): BrowserContext[] { return [this.context as unknown as BrowserContext]; }
  isConnected(): boolean { return this.connected; }
  async close(): Promise<void> { this.connected = false; this.context.page.closePage(); }
}

class ScriptedRuntime {
  scenario: Scenario = "success";
  agentCreations = 0;
  computerCreations = 0;
  observationCount = 0;
  private policyChecks = 0;

  reset(): void {
    this.scenario = "success";
    this.agentCreations = 0;
    this.computerCreations = 0;
    this.observationCount = 0;
    this.policyChecks = 0;
  }

  dependencies(): Partial<RuntimeDependencies> {
    return {
      createAgent: (_apiKey, _model, broker) => this.createAgent(broker),
      createSmolVM: () => this.createSmolVM(),
      connectOverCDP: async () => new FakeBrowser() as unknown as Browser,
    };
  }

  private createAgent(broker: ActionBroker): Agent {
    this.agentCreations += 1;
    const state = { messages: [] as Array<Record<string, unknown>>, errorMessage: undefined as string | undefined };
    return {
      state,
      prompt: async (prompt: string) => {
        if (prompt.includes("may have completed")) {
          await broker.runWebOperation({ kind: "observe" });
          state.messages.push({ role: "assistant", content: [{ type: "text", text: "I inspected the current page before deciding what to do next." }] });
          return;
        }
        if (prompt.includes("did not run")) {
          await broker.runWebOperation({ kind: "navigate", url: "https://example.com" });
          return;
        }
        if (prompt.includes("browser runner returned")) {
          state.messages.push({ role: "assistant", content: [{ type: "text", text: "The scripted browser opened Example Domain." }] });
          return;
        }
        await broker.runWebOperation({ kind: "navigate", url: "https://example.com" });
      },
      abort: () => undefined,
      waitForIdle: async () => undefined,
    } as unknown as Agent;
  }

  private createSmolVM() {
    return {
      computers: { create: async () => {
        this.computerCreations += 1;
        return this.createComputer();
      } },
      close: async () => undefined,
    } as unknown as ReturnType<RuntimeDependencies["createSmolVM"]>;
  }

  private createComputer(): NonNullable<ConversationContext["computer"]> {
    return {
      status: "ready", computerId: "computer-e2e", sandboxId: "sandbox-e2e", template: "linux-desktop", capabilities: [],
      display: { viewerUrl: "http://127.0.0.1:4320", vncUrl: "vnc://127.0.0.1:5900" },
      browser: { status: "ready", cdpUrl: "http://browser-e2e", launch: async () => undefined },
      files: { read: async () => "", write: async () => undefined, upload: async () => undefined, download: async () => undefined },
      exec: async (command: string[]) => {
        const program = Buffer.from(command[1] ?? "", "base64url").toString("utf8");
        if (program.includes("pageBindingRawUrl")) {
          this.policyChecks += 1;
          const binding = this.scenario === "failed_before_execution" && this.policyChecks > 1
            ? "https://example.org/changed"
            : "https://example.com/";
          return this.result({ binding, display: "https://example.com/" });
        }
        if (program.includes("const sensitivePath")) {
          this.observationCount += 1;
          return this.result({ title: "Example Domain", url: "https://example.com/", text: "Example Domain" });
        }
        if (this.scenario === "outcome_unknown") {
          return { ok: false, exitCode: 1, stdout: "", stderr: "scripted post-dispatch failure", durationMs: 1 };
        }
        return this.result({ navigated: true });
      },
      delete: async () => undefined,
    } as NonNullable<ConversationContext["computer"]>;
  }

  private result(programResult: unknown) {
    return {
      ok: true, exitCode: 0, stderr: "", durationMs: 1,
      stdout: `SMOLVM_BROWSER_RESULT=${JSON.stringify({ ok: true, value: { programResult, page: { title: "Example Domain", url: "https://example.com/" } } })}`,
    };
  }
}

const runtime = new ScriptedRuntime();
const directory = await mkdtemp(join(tmpdir(), "open-muse-e2e-"));
const store = new ConversationStateStore(join(directory, "state.json"));
let manager: ConversationManager;
let app: Server;
let managerGeneration = 0;

async function listen(server: Server, port: number): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
}

async function closeServer(server: Server | undefined): Promise<void> {
  if (!server?.listening) return;
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
}

async function openManager(): Promise<void> {
  manager = await ConversationManager.open("", "scripted", false, store, runtime.dependencies());
  managerGeneration += 1;
  app = createApp(manager);
  await listen(app, 4318);
}

async function reconstruct(): Promise<void> {
  await closeServer(app);
  await openManager();
}

async function reset(): Promise<void> {
  if (manager!) await manager.close().catch(() => undefined);
  if (app!) await closeServer(app);
  await unlink(store.path).catch(() => undefined);
  runtime.reset();
  await openManager();
}

await reset();

const controls = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", "http://127.0.0.1");
  const send = (status: number, value: unknown) => {
    response.statusCode = status;
    response.setHeader("content-type", "application/json; charset=utf-8");
    response.end(JSON.stringify(value));
  };
  if (request.method === "POST" && url.pathname === "/__e2e/reset") {
    await reset();
    return send(200, { reset: true, managerGeneration });
  }
  if (request.method === "POST" && url.pathname === "/__e2e/restart") {
    await manager.close();
    await reconstruct();
    return send(200, { restarted: true, managerGeneration });
  }
  if (request.method === "POST" && url.pathname === "/__e2e/scenario") {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const input = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}") as { scenario?: Scenario };
    if (!new Set(["success", "failed_before_execution", "outcome_unknown"]).has(input.scenario ?? "")) return send(400, { error: "Unknown scenario." });
    runtime.scenario = input.scenario!;
    return send(200, { scenario: runtime.scenario });
  }
  if (request.method === "GET" && url.pathname === "/__e2e/state") {
    const snapshot = manager.activeConversationId ? manager.snapshot(manager.activeConversationId) : undefined;
    const events = snapshot?.events ?? [];
    const journal = (manager as unknown as { context?: ConversationContext }).context?.operationJournal ?? [];
    return send(200, {
      managerGeneration,
      agentCreations: runtime.agentCreations,
      computerCreations: runtime.computerCreations,
      dispatchCount: events.filter((event) => event.type === "operation.dispatched").length,
      terminalCount: journal.filter((operation) => operation.state === "completed" || operation.state === "outcome_unknown").length,
      observationCount: runtime.observationCount,
      conversation: snapshot,
    });
  }
  send(404, { error: "Unknown E2E control route." });
});
await listen(controls, 4319);

const viewer = createServer((_request, response) => {
  response.setHeader("content-type", "text/html; charset=utf-8");
  response.end("<!doctype html><title>Scripted viewer</title><p>Deterministic browser viewer</p>");
});
await listen(viewer, 4320);
console.log("Deterministic OpenMuse manager harness ready at http://127.0.0.1:4318");

async function close(): Promise<void> {
  await manager.close().catch(() => undefined);
  await Promise.all([closeServer(app), closeServer(controls), closeServer(viewer)]);
  await rm(directory, { recursive: true, force: true });
  process.exit(0);
}
process.once("SIGINT", () => void close());
process.once("SIGTERM", () => void close());
