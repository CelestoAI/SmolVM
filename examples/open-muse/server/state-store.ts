import { randomUUID } from "node:crypto";
import { mkdir, open, readFile, rename, unlink, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { z } from "zod";
import type { ConversationContext, ConversationEvent, Message } from "./types.js";

const MAX_MESSAGES = 100;
const MAX_MESSAGE_BYTES = 64_000;
const MAX_EVENTS = 500;
const MAX_EVENT_SUMMARY_CHARACTERS = 1_000;

const messageSchema = z.object({
  id: z.string(),
  role: z.enum(["user", "assistant"]),
  text: z.string(),
  createdAt: z.string(),
});

const eventSchema = z.object({
  id: z.number().int().nonnegative(),
  conversationId: z.string(),
  stateVersion: z.number().int().nonnegative(),
  createdAt: z.string(),
  type: z.string(),
  payload: z.object({ summary: z.string().max(MAX_EVENT_SUMMARY_CHARACTERS).optional() }),
});

const storedConversationSchema = z.object({
  fileVersion: z.literal(1),
  conversation: z.object({
    id: z.string(),
    stateVersion: z.number().int().positive(),
    controlOwner: z.enum(["agent", "pause_requested", "human"]),
    runState: z.enum(["idle", "model_turn", "tool_action", "waiting_for_approval", "interrupted", "stopping", "stopped", "failed"]),
    sessionLifecycle: z.enum(["absent", "starting", "ready", "stopping", "deleted", "error"]),
    messages: z.array(messageSchema).max(MAX_MESSAGES),
    events: z.array(eventSchema).max(MAX_EVENTS),
    lastActivityAt: z.number().nonnegative(),
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

function safeEvent(event: ConversationEvent): StoredConversation["conversation"]["events"][number] {
  const summary = typeof event.payload.summary === "string"
    ? event.payload.summary.slice(0, MAX_EVENT_SUMMARY_CHARACTERS)
    : undefined;
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
    fileVersion: 1,
    conversation: {
      id: context.id,
      stateVersion: context.stateVersion,
      controlOwner: context.controlOwner,
      runState: context.runState,
      sessionLifecycle: context.sessionLifecycle,
      messages: boundedMessages(context.messages),
      events: context.events.slice(-MAX_EVENTS).map(safeEvent),
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
      return storedConversationSchema.parse(JSON.parse(contents));
    } catch {
      throw new Error(`Saved OpenMuse state is invalid at '${this.path}'. Run 'mv "${this.path}" "${this.path}.bad"', then run 'npm run dev'.`);
    }
  }

  save(state: StoredConversation): Promise<void> {
    const contents = `${JSON.stringify(storedConversationSchema.parse(state), null, 2)}\n`;
    const write = async () => {
      await mkdir(dirname(this.path), { recursive: true, mode: 0o700 });
      const temporaryPath = `${this.path}.${randomUUID()}.tmp`;
      try {
        await writeFile(temporaryPath, contents, { encoding: "utf8", mode: 0o600, flush: true });
        await rename(temporaryPath, this.path);
        if (process.platform !== "win32") {
          const directory = await open(dirname(this.path), "r");
          try {
            await directory.sync();
          } finally {
            await directory.close();
          }
        }
      } catch (error) {
        await unlink(temporaryPath).catch(() => undefined);
        throw error;
      }
    };
    const queued = this.writeQueue.catch(() => undefined).then(write);
    this.writeQueue = queued;
    return queued;
  }

  flush(): Promise<void> {
    return this.writeQueue;
  }
}
