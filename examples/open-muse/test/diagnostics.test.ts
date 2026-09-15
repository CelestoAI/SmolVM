import assert from "node:assert/strict";
import { request as httpRequest } from "node:http";
import type { AddressInfo } from "node:net";
import test from "node:test";
import type { Page } from "playwright-core";
import { createTab } from "../server/browser-tabs.js";
import { conversationDiagnostics } from "../server/diagnostics.js";
import { createApp } from "../server/index.js";
import { ConversationManager } from "../server/manager.js";
import { approveOperation, completeOperation, dispatchOperation, markOutcomeUnknown, recoverOperations } from "../server/operation-lifecycle.js";
import type { ConversationContext } from "../server/types.js";

const page = (url: string, closed = false) => ({ url: () => url, isClosed: () => closed }) as unknown as Page;

function populatedContext(manager: ConversationManager): ConversationContext {
  const context = (manager as unknown as { context: ConversationContext }).context;
  const success = completeOperation(
    dispatchOperation(approveOperation("browser_operation", "secret https://example.com/path?token=hidden", new Date(0), "success"), new Date(50)),
    "succeeded", undefined, new Date(100),
  );
  const safeFailure = completeOperation(
    approveOperation("browser_program", "private user text", new Date(200), "safe-failure"),
    "failed_before_execution", "TAB_CHANGED", new Date(250),
  );
  const unknown = markOutcomeUnknown(
    dispatchOperation(approveOperation("browser_program", "password=secret", new Date(300), "unknown"), new Date(320)),
    "field-value-must-never-escape", new Date(360),
  );
  const recovered = recoverOperations([
    success,
    safeFailure,
    unknown,
    approveOperation("checkout_review", "customer@example.com", new Date(400), "recovered"),
  ], new Date(450));
  context.operationJournal = recovered.journal;
  context.recovery = recovered.recovery;
  context.runState = "interrupted";
  context.sessionLifecycle = "ready";
  context.tabs.set("agent", createTab(page("https://private.example/?token=secret"), "agent", "epoch"));
  context.tabs.set("paused", createTab(page("https://example.com/paused"), "paused", "epoch"));
  context.tabs.set("human", createTab(page("https://example.com/human"), "human", "epoch"));
  context.tabs.set("popup", createTab(page("https://example.com/popup"), "quarantined", "epoch"));
  context.tabs.set("closed", createTab(page("https://example.com/closed", true), "agent", "epoch"));
  return context;
}

test("diagnostics count lifecycle, recovery, durations, and tabs without sensitive fields", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  await manager.create();
  const context = populatedContext(manager);

  const diagnostics = conversationDiagnostics(context);
  assert.deepEqual(diagnostics.operations.byState, { approved: 0, dispatched: 0, completed: 3, outcome_unknown: 1 });
  assert.deepEqual(diagnostics.operations.byOutcome, { succeeded: 1, failed_before_execution: 2 });
  assert.equal(diagnostics.operations.unknownCount, 1);
  assert.deepEqual(diagnostics.operations.completedDurationMs, { count: 3, total: 200, average: 200 / 3, latest: 50 });
  assert.equal(diagnostics.operations.lastSafeErrorCode, "PROCESS_RESTARTED");
  assert.deepEqual(diagnostics.tabs, { owned: 3, quarantined: 1 });
  assert.equal(diagnostics.runState, "interrupted");
  assert.equal(diagnostics.sessionLifecycle, "ready");

  const serialized = JSON.stringify(diagnostics);
  for (const forbidden of ["summary", "url", "secret", "password", "example.com", "customer@", "program", "field-value"]) {
    assert.doesNotMatch(serialized, new RegExp(forbidden, "i"));
  }
});

test("the diagnostics endpoint requires a loopback-authenticated session and stays redacted", async (t) => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  populatedContext(manager);
  const server = createApp(manager);
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise<void>((resolve, reject) => server.close((error) => error ? reject(error) : resolve())));
  const origin = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const path = `/api/conversations/${created.id}/diagnostics`;

  assert.equal((await fetch(`${origin}${path}`)).status, 401);
  const bootstrapResponse = await fetch(`${origin}/api/bootstrap`);
  const cookie = bootstrapResponse.headers.get("set-cookie")!.split(";")[0]!;
  const response = await fetch(`${origin}${path}`, { headers: { cookie } });
  assert.equal(response.status, 200);
  const body = await response.json() as Record<string, unknown>;
  assert.deepEqual((body.tabs as Record<string, number>), { owned: 3, quarantined: 1 });
  assert.doesNotMatch(JSON.stringify(body), /secret|example\.com|customer@|program|private user text/i);

  const port = (server.address() as AddressInfo).port;
  const nonLoopbackStatus = await new Promise<number | undefined>((resolve, reject) => {
    const request = httpRequest({ hostname: "127.0.0.1", port, path, headers: { cookie, host: "remote.example" } }, (response) => {
      response.resume();
      resolve(response.statusCode);
    });
    request.once("error", reject);
    request.end();
  });
  assert.equal(nonLoopbackStatus, 403);
});
