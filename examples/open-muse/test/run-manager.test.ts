import assert from "node:assert/strict";
import test from "node:test";
import type { SandboxClient, SandboxStatus, SmolVMClient } from "@celestoai/smolvm";
import type { Workflow } from "../server/agent.js";
import { RunManager } from "../server/run-manager.js";

class MemorySandbox implements SandboxClient {
  readonly id = "sbx-open-muse-test";
  status: SandboxStatus = "running";
  readonly calls: Array<readonly string[]> = [];
  readonly data = new Map<string, string>();
  readonly files = {
    read: async (path: string) => this.data.get(path) ?? "",
    write: async (path: string, content: string | Uint8Array) => { this.data.set(path, typeof content === "string" ? content : Buffer.from(content).toString("utf8")); },
    upload: async () => {},
    download: async () => {},
  };
  async exec(command: string | readonly string[]) {
    const argv = typeof command === "string" ? [command] : command;
    this.calls.push(argv);
    if (argv.some((part) => part.endsWith("verify_packet.py"))) return { ok: true, exitCode: 0, stdout: '{"ok":true,"errors":[]}', stderr: "", durationMs: 1 };
    if (argv.some((part) => part.endsWith("package_packet.py"))) this.data.set("/workspace/open-muse/output/packet.zip.b64", Buffer.from("zip").toString("base64"));
    return { ok: true, exitCode: 0, stdout: "", stderr: "", durationMs: 1 };
  }
  async delete() { this.status = "deleted"; }
}

class MemoryClient implements SmolVMClient {
  readonly sandbox = new MemorySandbox();
  closeCount = 0;
  readonly sandboxes = { create: async () => this.sandbox };
  async diagnose() { return { protocolVersion: 1, runtimeVersion: "test", nodeVersion: process.version, pythonVersion: "3", platform: "test", supported: true, problems: [] }; }
  async close() { this.closeCount += 1; await this.sandbox.delete(); }
}

const scripts = async () => ({ "fetch_page.py": "", "calculate_budget.py": "", "verify_packet.py": "", "package_packet.py": "" });
const goal = "Plan a three-day Bengaluru trip for two people.";

test("completes a fake run, exports all artifacts, and closes its client", async () => {
  const client = new MemoryClient();
  const workflow: Workflow = {
    plan: async () => ["Gather current evidence", "Compare walkable areas", "Verify the packet"],
    run: async (_goal, _constraints, _plan, state) => {
      const content: Record<string, string> = {
        "brief.md": "# Brief\nTotal INR 1100. Assumptions. Not verified. [source:S01]",
        "itinerary.md": "# Day 1\nA [source:S01]\n# Day 2\nB [source:S01]\n# Day 3\nC [source:S01]",
        "budget.csv": "category,item,quantity,unit_cost_inr,total_inr,source_id\nfood,meal,1,1000.00,1000.00,S01\ncontingency,10% contingency,1,100.00,100.00,\n",
        "sources.json": "[]\n",
      };
      for (const [name, value] of Object.entries(content)) await state.sandbox.files.write(`/workspace/open-muse/output/${name}`, value);
    },
  };
  const manager = new RunManager(workflow, () => client, scripts);
  const plan = await manager.createPlan(goal, []);
  const started = manager.start(plan.id);
  const finished = await waitFor(manager, started.id, "complete");
  assert.equal(finished.artifacts.length, 4);
  assert.equal(client.closeCount, 1);
  assert.equal(finished.cleanupConfirmed, true);
  assert.equal(Buffer.from(manager.packet(started.id)!).toString(), "zip");
  assert.ok(client.sandbox.calls.some((argv) => argv.some((part) => part.endsWith("verify_packet.py"))));
});

test("rejects a second active run and cancellation cleans up exactly once", async () => {
  const client = new MemoryClient();
  const workflow: Workflow = {
    plan: async () => ["Gather current evidence", "Compare walkable areas", "Verify the packet"],
    run: async (_goal, _constraints, _plan, state) => {
      if (state.signal.aborted) throw new DOMException("Aborted", "AbortError");
      return new Promise<void>((_resolve, reject) => {
        state.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      });
    },
  };
  const manager = new RunManager(workflow, () => client, scripts);
  const firstPlan = await manager.createPlan(goal, []);
  const first = manager.start(firstPlan.id);
  const secondPlan = await manager.createPlan(goal, []);
  assert.throws(() => manager.start(secondPlan.id), (error: unknown) => (error as { status?: number }).status === 409);
  manager.cancel(first.id);
  const cancelled = await waitFor(manager, first.id, "cancelled");
  assert.equal(cancelled.cleanupConfirmed, true);
  assert.equal(client.closeCount, 1);
});

test("workflow failures preserve a public failure event and still clean up", async () => {
  const client = new MemoryClient();
  const workflow: Workflow = {
    plan: async () => ["Gather current evidence", "Compare walkable areas", "Verify the packet"],
    run: async () => { throw new Error("private workflow detail"); },
  };
  const manager = new RunManager(workflow, () => client, scripts);
  const plan = await manager.createPlan(goal, []);
  const started = manager.start(plan.id);
  const failed = await waitFor(manager, started.id, "failed");

  assert.equal(failed.cleanupConfirmed, true);
  assert.equal(client.closeCount, 1);
  assert.equal(
    failed.events.find((event) => event.type === "run.failed")?.type,
    "run.failed",
  );
});

test("cleanup retries once before marking a successful run complete", async () => {
  class RetryCleanupClient extends MemoryClient {
    override async close() {
      this.closeCount += 1;
      if (this.closeCount === 1) throw new Error("busy");
      await this.sandbox.delete();
    }
  }
  const client = new RetryCleanupClient();
  const workflow: Workflow = {
    plan: async () => ["Gather current evidence", "Compare walkable areas", "Verify the packet"],
    run: async (_goal, _constraints, _plan, state) => {
      for (const [name, value] of Object.entries({
        "brief.md": "brief", "itinerary.md": "itinerary", "budget.csv": "budget", "sources.json": "[]",
      })) await state.sandbox.files.write(`/workspace/open-muse/output/${name}`, value);
    },
  };
  const manager = new RunManager(workflow, () => client, scripts);
  const plan = await manager.createPlan(goal, []);
  const started = manager.start(plan.id);
  const complete = await waitFor(manager, started.id, "complete");

  assert.equal(complete.cleanupConfirmed, true);
  assert.equal(client.closeCount, 2);
});

async function waitFor(manager: RunManager, id: string, phase: string) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const run = manager.get(id)!;
    if (run.phase === phase) return run;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`Run did not reach ${phase}.`);
}
