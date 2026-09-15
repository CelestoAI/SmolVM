import { randomUUID } from "node:crypto";

export type EffectOperationKind = "checkout_review" | "browser_program" | "browser_operation";
export type OperationState = "approved" | "dispatched" | "completed" | "outcome_unknown";
export type OperationOutcome = "succeeded" | "failed_before_execution";

export interface OperationRecord {
  id: string;
  kind: EffectOperationKind;
  summary: string;
  state: OperationState;
  createdAt: string;
  updatedAt: string;
  outcome?: OperationOutcome;
  errorCode?: string;
  recoveryAcknowledgedAt?: string;
}

export interface RecoveryState {
  kind: "failed_before_execution" | "outcome_unknown" | "interrupted";
  operationId?: string;
  summary?: string;
}

export const MAX_TERMINAL_OPERATIONS = 100;
export const MAX_OPERATION_SUMMARY_CHARACTERS = 1_000;

export function durableOperationSummary(kind: EffectOperationKind): string {
  switch (kind) {
    case "checkout_review": return "Open checkout review";
    case "browser_program": return "Run approved browser program";
    case "browser_operation": return "Run approved browser operation";
  }
}

export function redactOperationRecord(operation: OperationRecord): OperationRecord {
  return { ...operation, summary: durableOperationSummary(operation.kind) };
}

export function approveOperation(
  kind: EffectOperationKind,
  _summary: string,
  now = new Date(),
  id = `operation-${randomUUID()}`,
): OperationRecord {
  const timestamp = now.toISOString();
  return { id, kind, summary: durableOperationSummary(kind), state: "approved", createdAt: timestamp, updatedAt: timestamp };
}

export function dispatchOperation(operation: OperationRecord, now = new Date()): OperationRecord {
  if (operation.state !== "approved") throw new Error(`Cannot dispatch an operation in state '${operation.state}'.`);
  return { ...operation, state: "dispatched", updatedAt: now.toISOString() };
}

export function completeOperation(
  operation: OperationRecord,
  outcome: OperationOutcome,
  errorCode?: string,
  now = new Date(),
): OperationRecord {
  if (operation.state !== "approved" && operation.state !== "dispatched") {
    throw new Error(`Cannot complete an operation in state '${operation.state}'.`);
  }
  if (outcome === "succeeded" && operation.state !== "dispatched") {
    throw new Error("An operation cannot succeed before it is dispatched.");
  }
  return {
    ...operation,
    state: "completed",
    outcome,
    ...(errorCode ? { errorCode } : {}),
    updatedAt: now.toISOString(),
  };
}

export function markOutcomeUnknown(operation: OperationRecord, errorCode?: string, now = new Date()): OperationRecord {
  if (operation.state !== "dispatched") throw new Error(`Cannot mark an operation in state '${operation.state}' as unknown.`);
  return {
    ...operation,
    state: "outcome_unknown",
    ...(errorCode ? { errorCode } : {}),
    updatedAt: now.toISOString(),
  };
}

export function upsertOperation(journal: OperationRecord[], operation: OperationRecord): OperationRecord[] {
  const next = [...journal.filter((entry) => entry.id !== operation.id), operation];
  const active = next.filter((entry) => entry.state === "approved" || entry.state === "dispatched").slice(-1);
  const terminal = next.filter((entry) => entry.state === "completed" || entry.state === "outcome_unknown").slice(-MAX_TERMINAL_OPERATIONS);
  return [...terminal, ...active].sort((left, right) => left.createdAt.localeCompare(right.createdAt));
}

export function acknowledgeRecovery(journal: OperationRecord[], operationId: string | undefined, now = new Date()): OperationRecord[] {
  if (!operationId) return journal;
  return journal.map((operation) => operation.id === operationId
    ? { ...operation, recoveryAcknowledgedAt: now.toISOString() }
    : operation);
}

export function recoverOperations(journal: OperationRecord[], now = new Date()): {
  journal: OperationRecord[];
  recovery?: RecoveryState;
} {
  let recovery: RecoveryState | undefined;
  let recovered = journal;
  for (const operation of journal) {
    if (operation.state === "approved") {
      const completed = completeOperation(operation, "failed_before_execution", "PROCESS_RESTARTED", now);
      recovered = upsertOperation(recovered, completed);
      recovery = { kind: "failed_before_execution", operationId: completed.id, summary: completed.summary };
    } else if (operation.state === "dispatched") {
      const unknown = markOutcomeUnknown(operation, "PROCESS_RESTARTED", now);
      recovered = upsertOperation(recovered, unknown);
      recovery = { kind: "outcome_unknown", operationId: unknown.id, summary: unknown.summary };
    } else if (operation.state === "outcome_unknown" && !operation.recoveryAcknowledgedAt) {
      recovery = { kind: "outcome_unknown", operationId: operation.id, summary: operation.summary };
    } else if (operation.state === "completed" && operation.outcome === "failed_before_execution" && !operation.recoveryAcknowledgedAt) {
      recovery = { kind: "failed_before_execution", operationId: operation.id, summary: operation.summary };
    }
  }
  return { journal: recovered, recovery };
}
