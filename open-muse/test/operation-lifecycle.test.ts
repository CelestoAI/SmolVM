import assert from "node:assert/strict";
import test from "node:test";
import {
  acknowledgeRecovery,
  approveOperation,
  completeOperation,
  dispatchOperation,
  markOutcomeUnknown,
  recoverOperations,
  upsertOperation,
} from "../server/operation-lifecycle.js";

const start = new Date("2026-01-01T00:00:00.000Z");
const later = new Date("2026-01-01T00:00:01.000Z");

test("effect operation lifecycle permits only durable ordered transitions", () => {
  const approved = approveOperation("browser_program", "  Click Buy  ", start, "operation-test");
  const dispatched = dispatchOperation(approved, later);
  const completed = completeOperation(dispatched, "succeeded", undefined, later);

  assert.equal(approved.summary, "Run approved browser program");
  assert.equal(dispatched.state, "dispatched");
  assert.deepEqual({ state: completed.state, outcome: completed.outcome }, { state: "completed", outcome: "succeeded" });
  assert.throws(() => dispatchOperation(dispatched), /Cannot dispatch/);
  assert.throws(() => completeOperation(approved, "succeeded"), /cannot succeed before/);
  assert.throws(() => markOutcomeUnknown(approved), /Cannot mark/);
});

test("durable summaries are closed server-generated labels", () => {
  const approved = approveOperation(
    "browser_program",
    "Open https://example.com/search?q=private#results for person@example.com with 4111 1111 1111 1111",
    start,
    "operation-redacted",
  );

  assert.equal(approved.summary, "Run approved browser program");
  assert.doesNotMatch(approved.summary, /example|person|4111|private/i);
});

test("restart maps approved to safe failure and dispatched to unknown", () => {
  const approved = approveOperation("browser_operation", "Click Save", start, "approved");
  const dispatched = dispatchOperation(approveOperation("browser_program", "Submit form", start, "dispatched"), later);

  const safe = recoverOperations([approved], later);
  const unknown = recoverOperations([dispatched], later);

  assert.equal(safe.journal[0]?.outcome, "failed_before_execution");
  assert.equal(safe.recovery?.kind, "failed_before_execution");
  assert.equal(unknown.journal[0]?.state, "outcome_unknown");
  assert.equal(unknown.recovery?.kind, "outcome_unknown");
});

test("journal retains one active operation and the newest 100 terminal operations", () => {
  let journal = [] as ReturnType<typeof upsertOperation>;
  for (let index = 0; index < 105; index += 1) {
    const approved = approveOperation("browser_program", `Operation ${index}`, new Date(start.getTime() + index), `operation-${index}`);
    journal = upsertOperation(journal, completeOperation(approved, "failed_before_execution"));
  }
  journal = upsertOperation(journal, approveOperation("browser_program", "Active", later, "active"));

  assert.equal(journal.length, 101);
  assert.equal(journal.filter((operation) => operation.state === "approved").length, 1);
  assert.equal(journal.some((operation) => operation.id === "operation-0"), false);
});

test("acknowledged recovery does not reappear after another restart", () => {
  const dispatched = dispatchOperation(approveOperation("browser_program", "Submit", start, "operation-test"), later);
  const firstRecovery = recoverOperations([dispatched], later);
  const acknowledged = acknowledgeRecovery(firstRecovery.journal, firstRecovery.recovery?.operationId, later);

  assert.equal(recoverOperations(acknowledged, later).recovery, undefined);
});
