import assert from "node:assert/strict";
import test from "node:test";
import { ConversationManager } from "../server/manager.js";

// Regression: ISSUE-002 — refreshing always created a second active conversation
// Found by /qa on 2026-09-13
// Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-09-13.md
test("bootstrap callers can recover the active conversation", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = manager.create();
  assert.equal(manager.activeConversationId, created.id);
  assert.equal(manager.snapshot(manager.activeConversationId!).id, created.id);
  await manager.stop(created.id);
  assert.equal(manager.activeConversationId, undefined);
});
