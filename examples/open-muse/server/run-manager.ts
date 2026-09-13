import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { SmolVM, SmolVMError, type SandboxClient, type SmolVMClient, type SmolVMEvent } from "@celestoai/smolvm";
import { ARTIFACT_NAMES } from "./artifact-contract.js";
import type { Workflow } from "./agent.js";
import { redactedLog, toPublicError } from "./errors.js";
import { publicEvent, type ArtifactName, type PublicRun, type RunEvent, type RunEventInput, type RunPhase, type SourceRecord } from "./events.js";
import { finalizePacket, initializeSandbox, type ToolState } from "./tools.js";

interface PlanRecord {
  id: string;
  goal: string;
  constraints: string[];
  steps: string[];
}

interface RunContext {
  id: string;
  phase: RunPhase;
  goal: string;
  constraints: string[];
  plan: string[];
  startedAt: string;
  abortController: AbortController;
  client?: SmolVMClient;
  sandbox?: SandboxClient;
  artifacts: Partial<Record<ArtifactName, Uint8Array>>;
  zip?: Uint8Array;
  events: RunEvent[];
  listeners: Set<(event: RunEvent) => void>;
  cleanupConfirmed: boolean;
  task?: Promise<void>;
  retentionTimer?: ReturnType<typeof setTimeout>;
}

const DOWNLOAD_WINDOW_MS = 15 * 60_000;

export type SmolVMFactory = (onEvent: (event: SmolVMEvent) => void) => SmolVMClient;

export class RunManager {
  private readonly plans = new Map<string, PlanRecord>();
  private readonly runs = new Map<string, RunContext>();
  private active?: RunContext;
  private degraded = false;
  private nextEventId = 1;

  constructor(
    private readonly workflow: Workflow,
    private readonly clientFactory: SmolVMFactory = (onEvent) => new SmolVM({ onEvent, createTimeoutMs: 600_000 }),
    private readonly scriptLoader: () => Promise<Record<string, string>> = loadScripts,
    private readonly retentionMs = DOWNLOAD_WINDOW_MS,
  ) {}

  async createPlan(goal: string, constraints: string[]): Promise<PlanRecord> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 60_000);
    try {
      const plan = { id: randomUUID(), goal, constraints, steps: await this.workflow.plan(goal, constraints, controller.signal) };
      this.plans.set(plan.id, plan);
      return plan;
    } finally {
      clearTimeout(timer);
    }
  }

  start(planId: string): PublicRun {
    if (this.degraded) throw Object.assign(new Error("Cleanup needs attention before another run can start."), { status: 503 });
    if (this.active && !isTerminal(this.active.phase)) throw Object.assign(new Error("Open Muse is already working on a goal."), { status: 409 });
    const plan = this.plans.get(planId);
    if (!plan) throw Object.assign(new Error("That plan has expired. Create it again."), { status: 404 });
    this.plans.delete(planId);
    const context: RunContext = {
      id: randomUUID(),
      phase: "starting_sandbox",
      goal: plan.goal,
      constraints: [...plan.constraints],
      plan: [...plan.steps],
      startedAt: new Date().toISOString(),
      abortController: new AbortController(),
      artifacts: {},
      events: [],
      listeners: new Set(),
      cleanupConfirmed: false,
    };
    this.runs.set(context.id, context);
    this.active = context;
    this.emit(context, { type: "run.phase", phase: "starting_sandbox" });
    context.task = this.execute(context);
    return this.snapshot(context);
  }

  get(id: string): PublicRun | undefined {
    const context = this.runs.get(id);
    return context ? this.snapshot(context) : undefined;
  }

  subscribe(id: string, listener: (event: RunEvent) => void, afterId = 0): (() => void) | undefined {
    const context = this.runs.get(id);
    if (!context) return undefined;
    for (const event of context.events) if (event.id > afterId) listener(event);
    context.listeners.add(listener);
    return () => context.listeners.delete(listener);
  }

  addConstraint(id: string, constraint: string): PublicRun {
    const context = this.required(id);
    if (context.phase !== "starting_sandbox" && context.phase !== "researching") {
      throw Object.assign(new Error("Add constraints before Open Muse starts building the packet."), { status: 409 });
    }
    context.constraints.push(constraint);
    this.emit(context, { type: "run.warning", message: "New constraint queued for the next build step." });
    return this.snapshot(context);
  }

  cancel(id: string): PublicRun {
    const context = this.required(id);
    if (!isTerminal(context.phase)) context.abortController.abort();
    return this.snapshot(context);
  }

  artifact(id: string, name: ArtifactName): Uint8Array | undefined {
    return this.runs.get(id)?.artifacts[name];
  }

  packet(id: string): Uint8Array | undefined {
    return this.runs.get(id)?.zip;
  }

  async close(): Promise<void> {
    const context = this.active;
    if (!context || isTerminal(context.phase)) return;
    context.abortController.abort();
    await Promise.race([context.task, new Promise<void>((resolve) => setTimeout(resolve, 15_000))]);
  }

  private async execute(context: RunContext): Promise<void> {
    const deadline = setTimeout(() => context.abortController.abort(), 8 * 60_000);
    let outcome: "success" | "cancelled" | "failed" = "success";
    let failure: ReturnType<typeof toPublicError> | undefined;
    try {
      const scripts = await this.scriptLoader();
      context.client = this.clientFactory((event) => this.mapVmEvent(context, event));
      context.sandbox = await context.client.sandboxes.create({ network: { mode: "open" } });
      const state: ToolState = {
        sandbox: context.sandbox,
        signal: context.abortController.signal,
        sources: [] as SourceRecord[],
        findings: [],
        written: new Set(),
        emit: (event) => this.emit(context, event),
        phase: (phase) => this.phase(context, phase),
      };
      await initializeSandbox(state, scripts);
      this.phase(context, "researching");
      await this.workflow.run(context.goal, context.constraints, context.plan, state);
      this.phase(context, "verifying");
      const packet = await finalizePacket(state);
      this.phase(context, "exporting");
      context.artifacts = packet.files;
      context.zip = packet.zip;
    } catch (error) {
      if (context.abortController.signal.aborted) outcome = "cancelled";
      else {
        outcome = "failed";
        failure = toPublicError(error);
        const operation = error instanceof SmolVMError ? ` code=${error.code} operation=${error.operation}` : "";
        console.error(`Open Muse run failed phase=${context.phase}${operation}: ${redactedLog(error)}`);
      }
    } finally {
      clearTimeout(deadline);
      await this.capturePartialArtifacts(context);
      this.phase(context, "cleaning_up");
      try {
        await this.cleanup(context);
      } catch {
        outcome = "failed";
        this.degraded = true;
        const recovery = context.sandbox ? `smolvm sandbox delete ${context.sandbox.id}` : "smolvm doctor";
        this.emit(context, { type: "run.warning", message: "Cleanup needs attention.", recovery });
      }
    }

    if (outcome === "success") {
      this.phase(context, "complete");
      this.emit(context, { type: "run.completed", durationMs: Date.now() - Date.parse(context.startedAt) });
    } else if (outcome === "cancelled" && context.cleanupConfirmed) {
      this.phase(context, "cancelled");
    } else {
      this.phase(context, "failed");
      this.emit(context, { type: "run.failed", message: failure?.message ?? "Open Muse could not finish this research packet.", recovery: failure?.recovery });
    }
    this.scheduleRelease(context);
  }

  private async cleanup(context: RunContext): Promise<void> {
    if (!context.client) {
      context.cleanupConfirmed = true;
      return;
    }
    let firstError: unknown;
    try {
      await context.client.close();
      context.cleanupConfirmed = true;
      this.emit(context, { type: "vm.lifecycle", name: "Temporary computer deleted" });
      return;
    } catch (error) {
      firstError = error;
    }
    try {
      await context.client.close();
      context.cleanupConfirmed = true;
      this.emit(context, { type: "vm.lifecycle", name: "Temporary computer deleted" });
    } catch (error) {
      throw new AggregateError([firstError, error], "SmolVM cleanup failed twice");
    }
  }

  private async capturePartialArtifacts(context: RunContext): Promise<void> {
    if (!context.sandbox) return;
    let total = Object.values(context.artifacts).reduce((sum, bytes) => sum + (bytes?.byteLength ?? 0), 0);
    for (const name of ARTIFACT_NAMES) {
      if (context.artifacts[name]) continue;
      try {
        const content = await context.sandbox.files.read(`/workspace/open-muse/output/${name}`);
        const bytes = Buffer.from(content, "utf8");
        if (bytes.byteLength === 0 || bytes.byteLength > 1024 * 1024 || total + bytes.byteLength > 8 * 1024 * 1024) continue;
        context.artifacts[name] = bytes;
        total += bytes.byteLength;
      } catch {
        // A missing or partially written artifact is simply not recoverable.
      }
    }
  }

  private phase(context: RunContext, phase: RunPhase): void {
    context.phase = phase;
    this.emit(context, { type: "run.phase", phase });
  }

  private emit(context: RunContext, input: RunEventInput): void {
    const event = publicEvent(input, this.nextEventId++);
    context.events.push(event);
    if (context.events.length > 500) context.events.shift();
    for (const listener of context.listeners) listener(event);
  }

  private mapVmEvent(context: RunContext, event: SmolVMEvent): void {
    const progress = event.type === "image.download" && event.totalBytes
      ? Math.min(1, event.receivedBytes / event.totalBytes)
      : undefined;
    this.emit(context, { type: "vm.lifecycle", name: event.type, progress });
  }

  private snapshot(context: RunContext): PublicRun {
    return {
      id: context.id,
      phase: context.phase,
      goal: context.goal,
      constraints: [...context.constraints],
      plan: [...context.plan],
      startedAt: context.startedAt,
      events: [...context.events],
      artifacts: ARTIFACT_NAMES.flatMap((name) => {
        const bytes = context.artifacts[name];
        return bytes ? [{ name, bytes: bytes.byteLength }] : [];
      }),
      cleanupConfirmed: context.cleanupConfirmed,
    };
  }

  private scheduleRelease(context: RunContext): void {
    context.retentionTimer = setTimeout(() => {
      if (this.runs.get(context.id) !== context) return;
      context.listeners.clear();
      context.events.length = 0;
      context.artifacts = {};
      delete context.zip;
      delete context.task;
      this.runs.delete(context.id);
      if (this.active === context) this.active = undefined;
    }, this.retentionMs);
    context.retentionTimer.unref?.();
  }

  private required(id: string): RunContext {
    const context = this.runs.get(id);
    if (!context) throw Object.assign(new Error("That Open Muse run was not found."), { status: 404 });
    return context;
  }
}

function isTerminal(phase: RunPhase): boolean {
  return phase === "complete" || phase === "cancelled" || phase === "failed";
}

async function loadScripts(): Promise<Record<string, string>> {
  const names = ["search_web.py", "fetch_page.py", "calculate_budget.py", "verify_packet.py", "package_packet.py"];
  return Object.fromEntries(await Promise.all(names.map(async (name) => [name, await readFile(new URL(`./scripts/${name}`, import.meta.url), "utf8")])));
}
