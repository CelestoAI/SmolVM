import assert from "node:assert/strict";
import test from "node:test";
import type { Workflow } from "../server/agent.js";
import { RunManager } from "../server/run-manager.js";

test("packed SDK creates a real VM, exports a deterministic packet, and cleans up", {
  skip: process.env.OPEN_MUSE_REAL_VM !== "1" ? "set OPEN_MUSE_REAL_VM=1 on a supported host" : false,
  timeout: 10 * 60_000,
}, async () => {
  const workflow: Workflow = {
    plan: async () => ["Use deterministic evidence", "Write the four files", "Verify and export the packet"],
    run: async (_goal, _constraints, _plan, state) => {
      const source = [{ id: "S01", url: "https://example.com", finalUrl: "https://example.com", title: "Example Domain", retrievedAt: new Date().toISOString(), contentSha256: "a".repeat(64) }];
      await state.sandbox.files.write("/workspace/open-muse/output/sources.json", JSON.stringify(source));
      await state.sandbox.files.write("/workspace/open-muse/output/brief.md", "# Brief\nTotal INR 2200. Major assumptions apply.\n\n## Not verified\nAvailability. [source:S01]");
      await state.sandbox.files.write("/workspace/open-muse/output/itinerary.md", "# Day 1\nFood [source:S01]\n# Day 2\nArchitecture [source:S01]\n# Day 3\nWalk [source:S01]");
      await state.sandbox.files.write("/workspace/open-muse/research/budget-input.json", JSON.stringify([{ category: "food", item: "Meals", quantity: 2, unitCostInr: 1000, sourceId: "S01" }]));
      const result = await state.sandbox.exec(["python3", "/workspace/open-muse/scripts/calculate_budget.py", "/workspace/open-muse/research/budget-input.json", "/workspace/open-muse/output/budget.csv"]);
      assert.equal(result.ok, true, result.stderr);
    },
  };
  const manager = new RunManager(workflow);
  const plan = await manager.createPlan("Build the deterministic Open Muse smoke packet.", []);
  const started = manager.start(plan.id);
  for (let attempt = 0; attempt < 600; attempt += 1) {
    const run = manager.get(started.id)!;
    if (run.phase === "complete") {
      assert.equal(run.cleanupConfirmed, true);
      assert.equal(run.artifacts.length, 4);
      assert.ok(manager.packet(run.id)?.byteLength);
      return;
    }
    if (run.phase === "failed") assert.fail(run.events.find((event) => event.type === "run.failed")?.message ?? "real-VM smoke failed");
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  await manager.close();
  assert.fail("real-VM smoke timed out");
});
