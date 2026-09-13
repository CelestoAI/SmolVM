import assert from "node:assert/strict";
import test from "node:test";

// Regression: ISSUE-001 — CommonJS http-proxy used a named ESM import and crashed server startup
// Found by /qa on 2026-09-13
// Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-09-13.md
test("the server module loads in the Node ESM runtime", async () => {
  const server = await import("../server/index.js");
  assert.equal(typeof server.createApp, "function");
});
