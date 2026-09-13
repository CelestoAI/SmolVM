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

test("conversation creation and human control use single-owner state transitions", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = manager.create();
  assert.throws(
    () => manager.create(),
    (error: unknown) => (error as { status?: number }).status === 409,
  );

  const takeover = await manager.takeover(created.id);
  assert.equal(manager.snapshot(created.id).controlOwner, "human");
  assert.deepEqual(await manager.takeover(created.id), takeover);
  assert.throws(
    () => manager.resume(created.id, "wrong-control-epoch"),
    (error: unknown) => (error as { status?: number }).status === 409,
  );

  const resumed = manager.resume(created.id, takeover.controlEpoch);
  assert.equal(resumed.controlOwner, "agent");
  await manager.stop(created.id);
});
