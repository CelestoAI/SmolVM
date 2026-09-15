import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { z } from "zod";
import { MAX_OPERATION_SUMMARY_CHARACTERS, redactOperationRecord, upsertOperation } from "./operation-lifecycle.js";
import type { ConversationContext, ConversationEvent, Message } from "./types.js";
import { writePrivateFileAtomically } from "./private-file.js";

const MAX_MESSAGES = 100;
const MAX_MESSAGE_BYTES = 64_000;
const MAX_EVENTS = 500;
const MAX_EVENT_SUMMARY_CHARACTERS = 1_000;

const messageSchema = z.object({
  id: z.string(),
  role: z.enum(["user", "assistant"]),
  text: z.string(),
  createdAt: z.string(),
  turnId: z.string().optional(),
});

const eventSchema = z.object({
  id: z.number().int().nonnegative(),
  conversationId: z.string(),
  stateVersion: z.number().int().nonnegative(),
  createdAt: z.string(),
  type: z.string(),
  payload: z.object({ summary: z.string().max(MAX_EVENT_SUMMARY_CHARACTERS).optional() }),
});

const conversationV1Schema = z.object({
    id: z.string(),
    stateVersion: z.number().int().positive(),
    controlOwner: z.enum(["agent", "pause_requested", "human"]),
    runState: z.enum(["idle", "model_turn", "tool_action", "waiting_for_approval", "interrupted", "stopping", "stopped", "failed"]),
    sessionLifecycle: z.enum(["absent", "starting", "ready", "stopping", "deleted", "error"]),
    messages: z.array(messageSchema).max(MAX_MESSAGES),
    events: z.array(eventSchema).max(MAX_EVENTS),
  lastActivityAt: z.number().nonnegative(),
});

const operationSchema = z.object({
  id: z.string(),
  kind: z.enum(["checkout_review", "browser_program", "browser_operation"]),
  summary: z.string().max(MAX_OPERATION_SUMMARY_CHARACTERS),
  state: z.enum(["approved", "dispatched", "completed", "outcome_unknown"]),
  createdAt: z.string(),
  updatedAt: z.string(),
  outcome: z.enum(["succeeded", "failed_before_execution"]).optional(),
  errorCode: z.string().max(120).optional(),
  recoveryAcknowledgedAt: z.string().optional(),
});

const storedConversationV1Schema = z.object({
  fileVersion: z.literal(1),
  conversation: conversationV1Schema,
});

const storedConversationV2Schema = z.object({
  fileVersion: z.literal(2),
  conversation: conversationV1Schema.extend({ operationJournal: z.array(operationSchema).max(101) }),
});

const storedConversationV3Schema = z.object({
  fileVersion: z.literal(3),
  conversation: conversationV1Schema.extend({
    providerId: z.string().min(1).max(80),
    modelId: z.string().min(1).max(200),
    modelAccessState: z.enum(["ready", "auth_required", "model_unavailable"]),
    operationJournal: z.array(operationSchema).max(101),
  }),
});

const storedConversationSchema = z.object({
  fileVersion: z.literal(4),
  conversation: storedConversationV3Schema.shape.conversation.extend({
    recoveryTurn: z.object({ turnId: z.string(), userMessageId: z.string() }).optional(),
  }),
});

export type StoredConversation = z.infer<typeof storedConversationSchema>;

function boundedMessages(messages: Message[]): Message[] {
  const result: Message[] = [];
  let bytes = 0;
  for (const message of messages.slice(-MAX_MESSAGES).reverse()) {
    const messageBytes = Buffer.byteLength(message.text, "utf8");
    if (bytes + messageBytes > MAX_MESSAGE_BYTES) break;
    result.push({ ...message });
    bytes += messageBytes;
  }
  return result.reverse();
}

const DURABLE_EVENT_SUMMARIES: Readonly<Record<string, string>> = {
  "conversation.created": "Conversation ready",
  "conversation.stopping": "Conversation stopping",
  "conversation.stopped": "Conversation stopped",
  "conversation.interrupted": "Conversation interrupted",
  "conversation.continued": "Conversation continuing",
  "agent.started": "Agent turn started",
  "agent.completed": "Agent turn completed",
  "agent.failed": "Agent turn failed",
  "model.auth_required": "Model provider needs authentication",
  "model.reconnected": "Model provider reconnected",
  "model.switched": "Conversation model changed",
  "model.unavailable": "Conversation model unavailable",
  "browser.starting": "Browser starting",
  "browser.ready": "Browser ready",
  "browser.failed": "Browser failed",
  "browser.reconnecting": "Browser reconnecting",
  "browser.reconnected": "Browser reconnected",
  "browser.closed": "Browser closed",
  "tool.started": "Browser tool started",
  "tool.completed": "Browser tool completed",
  "tool.failed": "Browser tool failed",
  "approval.requested": "Browser approval requested",
  "approval.resolved": "Browser approval resolved",
  "approval.invalidated": "Browser approval invalidated",
  "operation.approved": "Browser operation approved",
  "operation.dispatched": "Browser operation dispatched",
  "operation.completed": "Browser operation completed",
  "operation.outcome_unknown": "Browser operation outcome unknown",
  "popup.quarantined": "Popup quarantined",
  "popup.adopted": "Popup adopted",
  "tab.opened": "Browser tab opened",
  "tab.navigated": "Browser tab navigated",
  "tab.closed": "Browser tab closed",
  "control.changed": "Browser control changed",
  "broker.decision": "Browser policy decision recorded",
  "cart.updated": "Cart updated",
};

function safeEvent(event: ConversationEvent): StoredConversation["conversation"]["events"][number] {
  const summary = DURABLE_EVENT_SUMMARIES[event.type];
  return {
    id: event.id,
    conversationId: event.conversationId,
    stateVersion: event.stateVersion,
    createdAt: event.createdAt,
    type: event.type,
    payload: summary === undefined ? {} : { summary },
  };
}

export function serializeConversation(context: ConversationContext): StoredConversation {
  return {
    fileVersion: 4,
    conversation: {
      id: context.id,
      stateVersion: context.stateVersion,
      controlOwner: context.controlOwner,
      runState: context.runState,
      providerId: context.providerId,
      modelId: context.modelId,
      modelAccessState: context.modelAccessState,
      sessionLifecycle: context.sessionLifecycle,
      messages: boundedMessages(context.messages),
      events: context.events.slice(-MAX_EVENTS).map(safeEvent),
      operationJournal: context.operationJournal.reduce(
        (journal, operation) => upsertOperation(journal, redactOperationRecord(operation)),
        [] as ConversationContext["operationJournal"],
      ),
      recoveryTurn: context.recoveryTurn,
      lastActivityAt: context.lastActivityAt,
    },
  };
}

export class ConversationStateStore {
  readonly path: string;
  private writeQueue: Promise<void> = Promise.resolve();

  constructor(path = process.env.OPEN_MUSE_STATE_PATH ?? ".open-muse/state.json") {
    this.path = resolve(path);
  }

  async load(): Promise<StoredConversation | undefined> {
    await this.writeQueue;
    let contents: string;
    try {
      contents = await readFile(this.path, "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined;
      throw error;
    }
    try {
      const parsed = JSON.parse(contents);
      const current = storedConversationSchema.safeParse(parsed);
      if (current.success) return {
        ...current.data,
        conversation: {
          ...current.data.conversation,
          events: current.data.conversation.events.map((event) => safeEvent(event as ConversationEvent)),
          operationJournal: current.data.conversation.operationJournal.map(redactOperationRecord),
        },
      };
      const legacyV3 = storedConversationV3Schema.safeParse(parsed);
      if (legacyV3.success) return {
        fileVersion: 4,
        conversation: {
          ...legacyV3.data.conversation,
          events: legacyV3.data.conversation.events.map((event) => safeEvent(event as ConversationEvent)),
          operationJournal: legacyV3.data.conversation.operationJournal.map(redactOperationRecord),
        },
      };
      const legacyV2 = storedConversationV2Schema.safeParse(parsed);
      if (legacyV2.success) return {
        fileVersion: 4,
        conversation: {
          ...legacyV2.data.conversation,
          providerId: "openai",
          modelId: process.env.OPENAI_MODEL ?? "gpt-5-mini",
          modelAccessState: "ready",
          events: legacyV2.data.conversation.events.map((event) => safeEvent(event as ConversationEvent)),
          operationJournal: legacyV2.data.conversation.operationJournal.map(redactOperationRecord),
        },
      };
      const legacyV1 = storedConversationV1Schema.safeParse(parsed);
      if (legacyV1.success) return {
        fileVersion: 4,
        conversation: {
          ...legacyV1.data.conversation,
          providerId: "openai",
          modelId: process.env.OPENAI_MODEL ?? "gpt-5-mini",
          modelAccessState: "ready",
          events: legacyV1.data.conversation.events.map((event) => safeEvent(event as ConversationEvent)),
          operationJournal: [],
        },
      };
      throw new Error("invalid state");
    } catch {
      throw new Error(`Saved OpenMuse state is invalid at '${this.path}'. Run 'mv "${this.path}" "${this.path}.bad"', then run 'npm run dev'.`);
    }
  }

  save(state: StoredConversation): Promise<void> {
    const contents = `${JSON.stringify(storedConversationSchema.parse(state), null, 2)}\n`;
    const write = () => writePrivateFileAtomically(this.path, contents);
    const queued = this.writeQueue.catch(() => undefined).then(write);
    this.writeQueue = queued;
    return queued;
  }

  flush(): Promise<void> {
    return this.writeQueue;
  }
}
