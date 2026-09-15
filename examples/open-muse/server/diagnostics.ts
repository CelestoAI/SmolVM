import type { OperationOutcome, OperationRecord, OperationState } from "./operation-lifecycle.js";
import type { ConversationContext } from "./types.js";

const OPERATION_STATES: OperationState[] = ["approved", "dispatched", "completed", "outcome_unknown"];
const OPERATION_OUTCOMES: OperationOutcome[] = ["succeeded", "failed_before_execution"];
const SAFE_ERROR_CODES = new Set([
  "APPROVED_CHECKPOINT_FAILED",
  "APPROVAL_STALE",
  "TAB_CHANGED",
  "PAGE_CHANGED",
  "COMPLETION_CHECKPOINT_FAILED",
  "EXECUTION_FAILED",
  "PRE_DISPATCH_FAILED",
  "PROCESS_RESTARTED",
]);

export interface ConversationDiagnostics {
  runState: ConversationContext["runState"];
  sessionLifecycle: ConversationContext["sessionLifecycle"];
  controlOwner: ConversationContext["controlOwner"];
  operations: {
    byState: Record<OperationState, number>;
    byOutcome: Record<OperationOutcome, number>;
    unknownCount: number;
    completedDurationMs: { count: number; total: number; average: number; latest?: number };
    lastSafeErrorCode?: string;
  };
  tabs: { owned: number; quarantined: number };
}

function durationMs(operation: OperationRecord): number | undefined {
  const createdAt = Date.parse(operation.createdAt);
  const updatedAt = Date.parse(operation.updatedAt);
  if (!Number.isFinite(createdAt) || !Number.isFinite(updatedAt) || updatedAt < createdAt) return;
  return updatedAt - createdAt;
}

export function conversationDiagnostics(
  context: Pick<ConversationContext, "runState" | "sessionLifecycle" | "controlOwner" | "operationJournal" | "tabs">,
): ConversationDiagnostics {
  const byState = Object.fromEntries(OPERATION_STATES.map((state) => [state, 0])) as Record<OperationState, number>;
  const byOutcome = Object.fromEntries(OPERATION_OUTCOMES.map((outcome) => [outcome, 0])) as Record<OperationOutcome, number>;
  for (const operation of context.operationJournal) {
    byState[operation.state] += 1;
    if (operation.outcome) byOutcome[operation.outcome] += 1;
  }

  const completed = context.operationJournal
    .filter((operation) => operation.state === "completed")
    .map((operation) => ({ operation, duration: durationMs(operation) }))
    .filter((entry): entry is { operation: OperationRecord; duration: number } => entry.duration !== undefined);
  const total = completed.reduce((sum, entry) => sum + entry.duration, 0);
  const latest = [...completed]
    .sort((left, right) => Date.parse(right.operation.updatedAt) - Date.parse(left.operation.updatedAt))[0]?.duration;
  const lastSafeErrorCode = context.operationJournal
    .filter((operation) => operation.errorCode && SAFE_ERROR_CODES.has(operation.errorCode))
    .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))[0]?.errorCode;

  let owned = 0;
  let quarantined = 0;
  for (const tab of context.tabs.values()) {
    if (tab.page.isClosed()) continue;
    if (tab.owner === "quarantined") quarantined += 1;
    else owned += 1;
  }

  return {
    runState: context.runState,
    sessionLifecycle: context.sessionLifecycle,
    controlOwner: context.controlOwner,
    operations: {
      byState,
      byOutcome,
      unknownCount: byState.outcome_unknown,
      completedDurationMs: {
        count: completed.length,
        total,
        average: completed.length ? total / completed.length : 0,
        ...(latest === undefined ? {} : { latest }),
      },
      ...(lastSafeErrorCode ? { lastSafeErrorCode } : {}),
    },
    tabs: { owned, quarantined },
  };
}
