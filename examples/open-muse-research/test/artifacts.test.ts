import assert from "node:assert/strict";
import test from "node:test";
import { validateArtifactName, validateMarkdown } from "../server/artifact-contract.js";
import { _test as agentTest } from "../server/agent.js";
import { _test as toolTest } from "../server/tools.js";

test("accepts the four fixed artifact names and rejects paths", () => {
  assert.equal(validateArtifactName("brief.md"), "brief.md");
  assert.throws(() => validateArtifactName("../brief.md"), /not allowed/);
  assert.throws(() => validateArtifactName("notes.txt"), /not allowed/);
});

test("requires three itinerary days and known citation markers", () => {
  const valid = "# Day 1\nFood [source:S01]\n# Day 2\nArchitecture [source:S01]\n# Day 3\nA walk [source:S01]";
  assert.doesNotThrow(() => validateMarkdown("itinerary.md", valid, new Set(["S01"])));
  assert.throws(() => validateMarkdown("itinerary.md", valid.replace("S01]", "S99]"), new Set(["S01"])), /unknown source/);
  assert.throws(() => validateMarkdown("itinerary.md", "# Day 1\n# Day 2", new Set()), /Day 1, Day 2, and Day 3/);
});

test("rejects private and ambiguous fetch destinations", () => {
  for (const url of ["http://example.com", "https://localhost/a", "https://service.local/a", "https://127.0.0.1/a", "https://intranet/a"]) {
    assert.throws(() => toolTest.publicUrl(url), /public HTTPS/);
  }
  assert.equal(toolTest.publicUrl("https://www.example.com/a").hostname, "www.example.com");
});

test("parses a strict three-step Pi planning response", () => {
  assert.deepEqual(agentTest.parsePlan('{"steps":["Gather current prices","Compare neighborhoods","Verify the final plan"]}'), [
    "Gather current prices", "Compare neighborhoods", "Verify the final plan",
  ]);
  const detailedStep = `Compare current lodging, transport, and meal prices across walkable Bengaluru neighborhoods, then record the best-supported tradeoffs for two travelers. ${"x".repeat(80)}`;
  assert.equal(agentTest.parsePlan(JSON.stringify({ steps: [detailedStep, detailedStep, detailedStep] }))[0], detailedStep);
  assert.throws(() => agentTest.parsePlan(JSON.stringify({ steps: ["x".repeat(601), detailedStep, detailedStep] })));
  assert.throws(() => agentTest.parsePlan('{"steps":["Only one step"]}'));
});

test("gives research one bounded correction turn when no tools were used", async () => {
  const state = { sources: [] as Array<{ id: string }>, findings: [] as Array<{ claim: string }> };
  let corrections = 0;
  await agentTest.collectResearchEvidence(
    state,
    async () => {},
    async () => {
      corrections += 1;
      state.sources.push({ id: "S01" });
      state.findings.push({ claim: "Supported claim" });
    },
  );
  assert.equal(corrections, 1);
  await assert.rejects(() => agentTest.collectResearchEvidence({ sources: [], findings: [] }, async () => {}, async () => {}), /enough sourced evidence/);
});

test("gives packet building one bounded correction turn for missing files", async () => {
  const state = { written: new Set(["brief.md", "budget.csv"] as const) };
  let requested: string[] = [];
  await agentTest.completePacket(state, async () => {}, async (missing) => {
    requested = missing;
    state.written.add("itinerary.md");
  });
  assert.deepEqual(requested, ["itinerary.md"]);
  await assert.rejects(
    () => agentTest.completePacket({ written: new Set() }, async () => {}, async () => {}),
    /brief.md, itinerary.md, budget.csv/,
  );
});
