import assert from "node:assert/strict";
import test from "node:test";
import { ActionBroker, MAX_BROWSER_PROGRAM_BYTES } from "../server/broker.js";
import type { ExecutableBrowserOperation } from "../server/browser-operations.js";
import { BrowserDriverError, type BrowserDriver } from "../server/browser-driver.js";
import { ConversationManager } from "../server/manager.js";
import type { ConversationContext } from "../server/types.js";
import type { TabTarget } from "../server/browser-tabs.js";

const locatorId = (index: number) => `00000000-0000-4000-8000-${index.toString().padStart(12, "0")}`;

function harness(
  persist: () => Promise<void> = async () => undefined,
  resolveTabTarget?: () => TabTarget,
  convertMarkdown?: (input: { title: string; url: string; text: string }) => Promise<string>,
) {
  const programs: string[] = [];
  const webOperations: ExecutableBrowserOperation[] = [];
  const expectedPages: Array<string | undefined> = [];
  const events: string[] = [];
  const eventPayloads: Record<string, unknown>[] = [];
  const context = {
    id: "conversation-test",
    stateVersion: 1,
    controlOwner: "agent",
    controlEpoch: "agent-control-test",
    runState: "idle",
    sessionLifecycle: "ready",
    messages: [],
    events: [],
    operationJournal: [],
    grants: [],
    cart: [],
    commerceRevision: 0,
    observationId: "obs-test",
    browserRefs: new Map([
      ["e1", { ref: "e1", role: "button", name: "Add to cart", publicName: "Add to cart", nth: 0, locatorId: locatorId(1), actionable: true, observationId: "obs-test", tabId: "legacy-tab", tabEpoch: 1, controlEpoch: "agent-control-test", pageBinding: "https://example.com" }],
      ["e2", { ref: "e2", role: "textbox", name: "Email", publicName: "Email", nth: 0, locatorId: locatorId(2), actionable: true, observationId: "obs-test", tabId: "legacy-tab", tabEpoch: 1, controlEpoch: "agent-control-test", pageBinding: "https://example.com" }],
      ["e3", { ref: "e3", role: "combobox", name: "Size", publicName: "Size", nth: 0, locatorId: locatorId(3), actionable: true, observationId: "obs-test", tabId: "legacy-tab", tabEpoch: 1, controlEpoch: "agent-control-test", pageBinding: "https://example.com" }],
      ["e4", { ref: "e4", role: "button", name: "First", publicName: "First", nth: 0, locatorId: locatorId(4), actionable: true, observationId: "obs-test", tabId: "legacy-tab", tabEpoch: 1, controlEpoch: "agent-control-test", pageBinding: "https://example.com" }],
      ["e5", { ref: "e5", role: "button", name: "Second", publicName: "Second", nth: 0, locatorId: locatorId(5), actionable: true, observationId: "obs-test", tabId: "legacy-tab", tabEpoch: 1, controlEpoch: "agent-control-test", pageBinding: "https://example.com" }],
      ["e6", { ref: "e6", role: "textbox", name: "Password", publicName: "Password", nth: 0, locatorId: locatorId(6), actionable: true, observationId: "obs-test", tabId: "legacy-tab", tabEpoch: 1, controlEpoch: "agent-control-test", pageBinding: "https://example.com" }],
      ["e7", { ref: "e7", role: "button", name: "Open", publicName: "Open", nth: 0, locatorId: locatorId(7), actionable: true, observationId: "obs-test", tabId: "legacy-tab", tabEpoch: 1, controlEpoch: "agent-control-test", pageBinding: "https://example.com" }],
    ]),
    lastActivityAt: Date.now(),
    receipts: new Map(),
    page: {
      url: () => "https://example.com",
      title: async () => "Example Domain",
      isClosed: () => false,
      context: () => ({ browser: () => ({ isConnected: () => true }) }),
    },
    computer: {
      sessionId: "browser-test",
      sandboxId: "vm-test",
      status: "ready",
      cdpUrl: "http://127.0.0.1:9222",
      async exec(command: string | readonly string[]) {
        const encoded = Array.isArray(command) ? command[1] : "";
        const program = Buffer.from(encoded, "base64url").toString("utf8");
        programs.push(program);
        const observation = {
          title: "Example Domain", url: "https://example.com", pageBinding: "https://example.com",
          snapshot: '- document "Example Domain"\n  - button "Add to cart" [ref=e1]\n  - textbox "Email" [ref=e2]\n  - combobox "Size" [ref=e3]',
          refs: [
            { ref: "e1", role: "button", name: "Add to cart", publicName: "Add to cart", nth: 0, locatorId: locatorId(1), actionable: true },
            { ref: "e2", role: "textbox", name: "Email", publicName: "Email", nth: 0, locatorId: locatorId(2), actionable: true },
            { ref: "e3", role: "combobox", name: "Size", publicName: "Size", nth: 0, locatorId: locatorId(3), actionable: true },
          ],
        };
        const programResult = program.includes("pageBindingRawUrl")
          ? { binding: "https://example.com", display: "https://example.com" }
          : program.includes("extractRawText")
            ? { title: "Example Domain", url: "https://example.com", pageBinding: "https://example.com", text: "iPhone 16 $799" }
          : program.includes("const observation =")
            ? (program.includes("mouse.wheel") ? { scrolled: "down", observation } : observation)
            : { title: "Example Domain" };
        return {
          ok: true, exitCode: 0,
          stdout: `SMOLVM_BROWSER_RESULT=${JSON.stringify({ ok: true, value: { programResult, page: { title: "Example Domain", url: "https://example.com" } } })}\n`,
          stderr: "", durationMs: 1,
        };
      },
      async delete() {},
    },
  } as ConversationContext;
  const browserDriver: BrowserDriver = {
    inspect: async () => ({ binding: "https://example.com", display: "https://example.com" }),
    execute: async (_page, operation, expectedPage) => {
      webOperations.push(operation);
      expectedPages.push(expectedPage);
      const observation = {
        title: "Example Domain", url: "https://example.com", pageBinding: "https://example.com",
        snapshot: '- document "Example Domain"\n  - button "Add to cart" [ref=e1]\n  - textbox "Email" [ref=e2]\n  - combobox "Size" [ref=e3]',
        refs: [
          { ref: "e1", role: "button", name: "Add to cart", publicName: "Add to cart", nth: 0, locatorId: locatorId(1), actionable: true },
          { ref: "e2", role: "textbox", name: "Email", publicName: "Email", nth: 0, locatorId: locatorId(2), actionable: true },
          { ref: "e3", role: "combobox", name: "Size", publicName: "Size", nth: 0, locatorId: locatorId(3), actionable: true },
        ],
      };
      if (operation.kind === "observe") return observation;
      if (operation.kind === "scroll") return { scrolled: operation.direction, observation };
      if (operation.kind === "extract") return { title: "Example Domain", url: "https://example.com", pageBinding: "https://example.com", text: "iPhone 16 $799" };
      if (operation.kind === "navigate") return { opened: operation.url, observation };
      if (operation.kind === "click") return { clicked: true };
      if (operation.kind === "fill") return { filled: true };
      if (operation.kind === "select") return { selected: operation.label };
      return { pressed: operation.key };
    },
  };
  const broker = new ActionBroker(context, async () => undefined, (type, payload) => { events.push(type); eventPayloads.push(payload); }, persist, resolveTabTarget, convertMarkdown, browserDriver);
  return { broker, context, programs, webOperations, expectedPages, events, eventPayloads, browserDriver };
}

test("read-only browser programs wait for one-time approval", async () => {
  const { broker, programs, events } = harness();

  const pending = await broker.runProgram("return { title: await page.title() };", false, "Read the title");

  assert.equal(pending.approvalRequired, true);
  assert.equal(programs.length, 0);
  assert.deepEqual(events, ["approval.requested"]);
});

test("active browser programs wait for one-time approval", async () => {
  const { broker, context, programs } = harness();

  const pending = await broker.runProgram("await page.getByRole('link').click();", true, "Open the selected link");

  assert.equal(pending.approvalRequired, true);
  assert.equal(context.runState, "waiting_for_approval");
  assert.equal(programs.length, 0);
  assert.ok(context.pendingApproval);

  const outcome = await broker.resolveApproval(
    context.pendingApproval.approvalId,
    context.pendingApproval.actionDigest,
    true,
  );

  assert.deepEqual(outcome, {
    resumeAgent: true,
    browserResult: {
      programResult: { title: "Example Domain" },
      page: { title: "Example Domain", url: "https://example.com" },
    },
  });
  assert.equal(programs.length, 1);
  assert.doesNotMatch(programs[0], /visibleText/);
  assert.match(programs[0], /parsedUrl\.origin.*parsedUrl\.pathname/);
  assert.equal(context.pendingApproval, undefined);
});

test("navigation programs also require approval", async () => {
  const { broker, context, programs } = harness();

  const pending = await broker.runProgram(
    "await page.goto('https://www.amazon.in'); return { title: await page.title() };",
    true,
    "Open Amazon India and read its title",
  );

  assert.equal(pending.approvalRequired, true);
  assert.equal(programs.length, 0);
  await broker.resolveApproval(
    context.pendingApproval!.approvalId,
    context.pendingApproval!.actionDigest,
    true,
  );
  assert.equal(programs.length, 1);
});

test("passive browser operations use host Playwright without approval", async () => {
  const { broker, context, programs, webOperations, events } = harness();

  const observed = await broker.runWebOperation({ kind: "observe" });
  await broker.runWebOperation({ kind: "scroll", direction: "down" });

  assert.equal(context.pendingApproval, undefined);
  assert.equal(programs.length, 0);
  assert.deepEqual(webOperations, [{ kind: "observe" }, { kind: "scroll", direction: "down" }]);
  assert.equal(observed.title, "Example Domain");
  assert.match(String(observed.snapshot), /Add to cart/);
  assert.deepEqual(events, ["tool.started", "tool.completed", "tool.started", "tool.completed"]);
});

test("page extraction uses Markdown conversion and falls back to raw redacted text", async () => {
  const converted = harness(async () => undefined, undefined, async ({ text }) => `# Products\n\n${text}`);
  assert.deepEqual(await converted.broker.runWebOperation({ kind: "extract" }), {
    title: "Example Domain", url: "https://example.com", markdown: "# Products\n\niPhone 16 $799", source: "gpt-5-nano",
  });

  const fallback = harness(async () => undefined, undefined, async () => { throw new Error("model unavailable"); });
  const result = await fallback.broker.runWebOperation({ kind: "extract" });
  assert.equal(result.markdown, "iPhone 16 $799");
  assert.equal(result.source, "raw-fallback");
  assert.match(String(result.warning), /conversion failed/);

  const unavailable = harness();
  assert.deepEqual(await unavailable.broker.runWebOperation({ kind: "extract" }), {
    title: "Example Domain", url: "https://example.com", markdown: "iPhone 16 $799", source: "raw-fallback",
    warning: "Markdown conversion was unavailable, so this is the raw redacted page text.",
  });

  const empty = harness(async () => undefined, undefined, async () => "should not run");
  empty.browserDriver.execute = async () => ({ title: "Empty", url: "https://example.com", pageBinding: "https://example.com", text: "" });
  assert.deepEqual(await empty.broker.runWebOperation({ kind: "extract" }), {
    title: "Empty", url: "https://example.com", markdown: "", source: "raw-fallback",
  });
});

test("scoped extraction resolves the latest ref without requiring an interactive target", async () => {
  const scoped = harness(async () => undefined, undefined, async ({ text }) => text);
  scoped.context.browserRefs.set("e8", {
    ref: "e8", role: "region", name: "Products", publicName: "Products", nth: 0, locatorId: locatorId(8),
    actionable: false, observationId: "obs-test", tabId: "legacy-tab", tabEpoch: 1,
    controlEpoch: "agent-control-test", pageBinding: "https://example.com",
  });

  await scoped.broker.runWebOperation({ kind: "extract", scopeRef: "e8" });

  assert.deepEqual(scoped.webOperations[0], {
    kind: "extract", scopeRef: "e8",
    target: { role: "region", name: "Products", publicName: "Products", nth: 0, locatorId: locatorId(8) },
  });
  assert.equal(scoped.context.pendingApproval, undefined);
});

test("page capture failures are not returned as successful empty data", async () => {
  const observation = harness();
  observation.browserDriver.execute = async () => { throw new BrowserDriverError("ACCESSIBILITY_CAPTURE_FAILED", "Chromium could not produce an accessibility snapshot; try again or use Take control."); };
  await assert.rejects(
    () => observation.broker.runWebOperation({ kind: "observe" }),
    (error: unknown) => (error as { code?: string }).code === "ACCESSIBILITY_CAPTURE_FAILED" && /accessibility snapshot/.test((error as Error).message),
  );
  assert.equal(observation.eventPayloads.at(-1)?.errorCode, "ACCESSIBILITY_CAPTURE_FAILED");

  const extraction = harness();
  extraction.browserDriver.execute = async () => { throw new BrowserDriverError("TEXT_EXTRACTION_FAILED", "Chromium could not extract text from the page; try again or use Take control."); };
  await assert.rejects(() => extraction.broker.runWebOperation({ kind: "extract" }), /could not extract text/);
});

test("active browser operations create page-bound one-shot approvals", async () => {
  const { broker, context, programs, webOperations, expectedPages, events } = harness();

  const pending = await broker.runWebOperation({
    kind: "click",
    ref: "e1",
  });

  assert.equal(pending.approvalRequired, true);
  assert.equal(pending.pageUrl, "https://example.com");
  assert.equal("pageBinding" in pending, false);
  assert.deepEqual(pending.operation, { kind: "click", ref: "e1" });
  assert.equal(programs.length, 0);
  assert.equal(webOperations.length, 0);
  assert.equal(context.runState, "waiting_for_approval");
  assert.equal(events.at(-1), "approval.requested");

  const approval = context.pendingApproval!;
  const outcome = await broker.resolveApproval(approval.approvalId, approval.actionDigest, true);

  assert.equal(programs.length, 0);
  assert.deepEqual(webOperations, [{
    kind: "click", ref: "e1",
    target: { role: "button", name: "Add to cart", publicName: "Add to cart", nth: 0, locatorId: locatorId(1) },
  }]);
  assert.deepEqual(expectedPages, ["https://example.com"]);
  assert.equal(context.pendingApproval, undefined);
  assert.equal(context.runState, "idle");
  assert.equal("browserResult" in outcome, true);
});

test("browser refs expire when the tab epoch changes", async () => {
  let target: TabTarget = { id: "legacy-tab", epoch: 1, controlEpoch: "agent-control-test", pageIndex: 0, pageBinding: "https://example.com" };
  const { broker, context } = harness(async () => undefined, () => target);
  target = { ...target, epoch: 2 };

  await assert.rejects(() => broker.runWebOperation({ kind: "click", ref: "e1" }), /ref is stale/);
  assert.equal(context.pendingApproval, undefined);
});

test("operation policy rejects private navigation and sensitive fields", async () => {
  const { broker, programs } = harness();

  await assert.rejects(() => broker.runWebOperation({ kind: "navigate", url: "http://127.0.0.1/admin" }), /private or local/);
  await assert.rejects(() => broker.runWebOperation({ kind: "navigate", url: "http://169.254.169.254/latest/meta-data" }), /private or local/);
  await assert.rejects(() => broker.runWebOperation({ kind: "navigate", url: "http://[::1]/admin" }), /private or local/);
  await assert.rejects(() => broker.runWebOperation({ kind: "navigate", url: "file:///etc/passwd" }), /public HTTP or HTTPS/);
  await assert.rejects(() => broker.runWebOperation({ kind: "fill", ref: "e6", value: "secret" }), /Take control/);
  await assert.rejects(() => broker.runWebOperation({ kind: "click", ref: "bad" } as never), /ref is invalid/);
  await assert.rejects(() => broker.runWebOperation({ kind: "select", ref: "e3" } as never), /option label/);
  assert.equal(programs.length, 0);
});

test("operation approvals bind URL queries without exposing them in the approval card", async () => {
  const { broker, context, programs, expectedPages, browserDriver } = harness();
  browserDriver.inspect = async () => ({ binding: "https://example.com/search?q=private#results", display: "https://example.com/search" });

  const pending = await broker.runWebOperation({ kind: "click", ref: "e7" });

  assert.equal(pending.pageUrl, "https://example.com/search");
  assert.equal(JSON.stringify(pending).includes("q=private"), false);
  assert.equal(context.pendingApproval?.pageBinding, "https://example.com/search?q=private#results");

  await broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true);
  assert.equal(programs.length, 0);
  assert.deepEqual(expectedPages, ["https://example.com/search?q=private#results"]);
});

test("concurrent active operations share the first pending approval", async () => {
  const { broker, context, browserDriver } = harness();
  const originalInspect = browserDriver.inspect.bind(browserDriver);
  browserDriver.inspect = async (page) => {
    await new Promise((resolve) => setTimeout(resolve, 5));
    return originalInspect(page);
  };

  const [first, second] = await Promise.all([
    broker.runWebOperation({ kind: "click", ref: "e4" }),
    broker.runWebOperation({ kind: "click", ref: "e5" }),
  ]);

  assert.equal(first.approvalId, second.approvalId);
  assert.deepEqual(context.pendingApproval?.operation, { kind: "click", ref: "e4", target: { role: "button", name: "First", nth: 0, locatorId: locatorId(4), publicName: "First" } });
});

test("approval events do not retain browser field values", async () => {
  const { broker, context, eventPayloads } = harness();

  const pending = await broker.runWebOperation({
    kind: "fill",
    ref: "e2",
    value: "person@example.com",
  });

  assert.equal(JSON.stringify(pending).includes("person@example.com"), false);
  assert.equal(JSON.stringify(eventPayloads.at(-1)).includes("person@example.com"), false);
  assert.equal(context.pendingApproval?.operation?.kind, "fill");
  assert.equal(context.pendingApproval?.operation?.kind === "fill" ? context.pendingApproval.operation.value : undefined, "person@example.com");
});

test("conversation snapshots hide private page bindings and fill values", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const context = (manager as unknown as { context: ConversationContext }).context;
  context.pendingApproval = {
    kind: "browser_operation",
    approvalId: "approval-test",
    actionDigest: "digest-test",
    reason: "Fill textbox “Email”",
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    operation: { kind: "fill", ref: "e2", target: { role: "textbox", name: "Email", nth: 0, locatorId: locatorId(2) }, value: "person@example.com" },
    pageUrl: "https://example.com/search",
    pageBinding: "https://example.com/search?q=private#results",
  };

  const snapshot = manager.snapshot(created.id);

  assert.equal(snapshot.pendingApproval?.pageUrl, "https://example.com/search");
  assert.deepEqual(snapshot.pendingApproval?.operation, { kind: "fill", ref: "e2" });
  assert.equal(context.pendingApproval.operation?.kind === "fill" ? context.pendingApproval.operation.value : undefined, "person@example.com");
  assert.equal("pageBinding" in snapshot.pendingApproval!, false);
  assert.equal(JSON.stringify(snapshot).includes("q=private"), false);
  assert.equal(JSON.stringify(snapshot).includes("person@example.com"), false);
});

test("browser programs never retry or return page data after an uncertain failure", async () => {
  const { broker, context, programs, events } = harness();
  let executions = 0;
  context.computer!.exec = async (command: string | readonly string[]) => {
    const encoded = Array.isArray(command) ? command[1] : "";
    programs.push(Buffer.from(encoded, "base64url").toString("utf8"));
    executions += 1;
    if (executions === 1) {
      return {
        ok: false,
        exitCode: 1,
        stdout: "",
        stderr: "Locator wait exceeded the per-operation limit.",
        durationMs: 10_000,
      };
    }
    return {
      ok: true,
      exitCode: 0,
      stdout: `SMOLVM_BROWSER_RESULT=${JSON.stringify({
        ok: true,
        value: {
          title: "Amazon.com : iPhone",
          url: "https://www.amazon.com/s",
          visibleText: "Results iPhone $799.00 $899.00",
        },
      })}\n`,
      stderr: "",
      durationMs: 20_000,
    };
  };

  await broker.runProgram(
    "await page.goto('https://www.amazon.com'); await page.waitForSelector('#missing'); return [];",
    false,
    "Search Amazon for iPhone prices",
  );
  const outcome = await broker.resolveApproval(
    context.pendingApproval!.approvalId,
    context.pendingApproval!.actionDigest,
    true,
  );

  assert.match(programs[0], /setDefaultTimeout\(10_000\)/);
  assert.match(programs[0], /setDefaultNavigationTimeout\(15_000\)/);
  assert.doesNotMatch(programs[0], /Promise\.race/);
  assert.doesNotMatch(programs[0], /visibleText/);
  assert.equal(programs.length, 1);
  assert.deepEqual(outcome, {
    resumeAgent: false,
    recovery: { kind: "outcome_unknown", operationId: context.operationJournal.at(-1)?.id, summary: "Run approved browser program" },
  });
  assert.equal(events.at(-1), "operation.outcome_unknown");
  assert.equal(context.runState, "interrupted");
  assert.equal(context.operationJournal.at(-1)?.state, "outcome_unknown");
});

test("browser programs reject invalid input and mark malformed post-dispatch results unknown", async () => {
  const initial = harness();

  await assert.rejects(() => initial.broker.runProgram(" ", false, "Empty"), /1 to 18,000 bytes/);
  await assert.rejects(
    () => initial.broker.runProgram("x".repeat(MAX_BROWSER_PROGRAM_BYTES + 1), false, "Oversized"),
    /1 to 18,000 bytes/,
  );

  const originalError = console.error;
  console.error = () => undefined;
  try {
    for (const stdout of ["unexpected output", "SMOLVM_BROWSER_RESULT={not-json}\n", 'SMOLVM_BROWSER_RESULT={"ok":false}\n']) {
      const { broker, context } = harness();
      context.computer!.exec = async () => ({ ok: true, exitCode: 0, stdout, stderr: "", durationMs: 1 });
      await broker.runProgram("return true;", false, "Malformed runner");
      const outcome = await broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true);
      assert.equal(outcome.resumeAgent, false);
      assert.equal(context.recovery?.kind, "outcome_unknown");
    }
  } finally {
    console.error = originalError;
  }
});

test("messages are rejected while the user controls the browser", async () => {
  const manager = new ConversationManager("", "gpt-5-mini");
  const created = await manager.create();
  const context = (manager as unknown as { context: ConversationContext }).context;
  context.controlOwner = "pause_requested";

  await assert.rejects(
    manager.send(created.id, "try again"),
    (error: unknown) => (error as { status?: number }).status === 409
      && (error as Error).message === "Wait for browser control to finish transferring, then send the message again.",
  );

  context.controlOwner = "agent";
  await manager.takeover(created.id);

  await assert.rejects(
    manager.send(created.id, "try again"),
    (error: unknown) => (error as { status?: number }).status === 409
      && (error as Error).message === "Select Return control before sending a message to OpenMuse.",
  );
  assert.deepEqual(manager.snapshot(created.id).messages, []);
  await manager.stop(created.id);
});

test("website approvals can be denied and stale approvals cannot execute", async () => {
  const denied = harness();
  await denied.broker.runProgram("await page.locator('button').click();", true, "Click once");
  const pending = denied.context.pendingApproval!;

  assert.deepEqual(
    await denied.broker.resolveApproval(pending.approvalId, pending.actionDigest, false),
    { resumeAgent: false },
  );
  assert.equal(denied.programs.length, 0);
  assert.equal(denied.context.runState, "idle");

  const stale = harness();
  await stale.broker.runProgram("await page.locator('button').click();", true, "Click once");
  stale.context.pendingApproval!.expiresAt = new Date(Date.now() - 1).toISOString();
  const staleOutcome = await stale.broker.resolveApproval(
    stale.context.pendingApproval!.approvalId,
    stale.context.pendingApproval!.actionDigest,
    true,
  );
  assert.equal(staleOutcome.resumeAgent, false);
  assert.equal(stale.context.recovery?.kind, "failed_before_execution");
  assert.equal(stale.programs.length, 0);
});

test("checkpoint failures before computer.exec are safe and never dispatch", async () => {
  let checkpoints = 0;
  const { broker, context, programs, events } = harness(async () => {
    checkpoints += 1;
    if (checkpoints === 2) throw new Error("state disk unavailable");
  });
  await broker.runProgram("return { changed: true };", true, "Change the page");

  const outcome = await broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true);

  assert.equal(programs.length, 0);
  assert.equal(outcome.resumeAgent, false);
  assert.equal(context.recovery?.kind, "failed_before_execution");
  assert.equal(context.operationJournal.at(-1)?.outcome, "failed_before_execution");
  assert.equal(events.includes("operation.dispatched"), false);
  assert.equal(events.includes("tool.started"), false);
});

test("completion checkpoint failures become unknown after one execution", async () => {
  let checkpoints = 0;
  const { broker, context, programs, events } = harness(async () => {
    checkpoints += 1;
    if (checkpoints === 3) throw new Error("state disk unavailable");
  });
  await broker.runProgram("return { changed: true };", true, "Change the page");

  const outcome = await broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true);

  assert.equal(programs.length, 1);
  assert.equal(outcome.resumeAgent, false);
  assert.equal(context.recovery?.kind, "outcome_unknown");
  assert.equal(context.operationJournal.at(-1)?.state, "outcome_unknown");
  assert.equal(events.includes("operation.completed"), false);
  assert.equal(events.includes("approval.resolved"), false);
});

test("approvals bind to one tab epoch and stale tabs fail before execution", async () => {
  let target: TabTarget = {
    id: "tab-one",
    epoch: 3,
    controlEpoch: "control-one",
    pageIndex: 2,
    pageBinding: "https://example.com/form?draft=1",
    pageUrl: "https://example.com/form",
  };
  const { broker, context, programs } = harness(async () => undefined, () => target);
  const pending = await broker.runProgram("return { submitted: true };", true, "Submit form");

  assert.equal(pending.approvalRequired, true);
  assert.equal(context.pendingApproval?.tabId, "tab-one");
  assert.equal(context.pendingApproval?.tabEpoch, 3);
  assert.equal(programs.length, 0);

  target = { ...target, epoch: 4 };
  const outcome = await broker.resolveApproval(context.pendingApproval!.approvalId, context.pendingApproval!.actionDigest, true);

  assert.equal(outcome.resumeAgent, false);
  assert.equal(context.recovery?.kind, "failed_before_execution");
  assert.equal(context.operationJournal.at(-1)?.errorCode, "TAB_CHANGED");
  assert.equal(programs.length, 0);
});

test("structured operations use the active host page without computer.exec", async () => {
  const target: TabTarget = { id: "tab-three", epoch: 1, controlEpoch: "control-one", pageIndex: 2 };
  const { broker, programs, webOperations } = harness(async () => undefined, () => target);

  await broker.runWebOperation({ kind: "observe" });

  assert.equal(programs.length, 0);
  assert.deepEqual(webOperations, [{ kind: "observe" }]);
});

test("raw programs re-check the approved page inside the dispatched runner", async () => {
  const target: TabTarget = {
    id: "tab-bound", epoch: 1, controlEpoch: "agent-control-test", pageIndex: 0,
    pageBinding: "https://example.com/form?draft=secret", pageUrl: "https://example.com/form",
  };
  const { broker, context, programs, browserDriver } = harness(async () => undefined, () => target);
  browserDriver.inspect = async () => ({ binding: "https://example.com/form?draft=secret", display: "https://example.com/form" });
  context.computer!.exec = async (command: string | readonly string[]) => {
    const encoded = Array.isArray(command) ? command[1] : "";
    const program = Buffer.from(encoded, "base64url").toString("utf8");
    programs.push(program);
    return { ok: false, exitCode: 1, stdout: "", stderr: "The approved page changed before execution.", durationMs: 1 };
  };

  await broker.runProgram("return { changed: true };", true, "model supplied secret https://example.com/private");
  const pending = context.pendingApproval!;
  const outcome = await broker.resolveApproval(pending.approvalId, pending.actionDigest, true);

  assert.equal(outcome.resumeAgent, false);
  assert.equal(context.operationJournal.at(-1)?.state, "outcome_unknown");
  assert.equal(context.operationJournal.at(-1)?.summary, "Run approved browser program");
  assert.equal(programs.length, 1);
  assert.ok(programs[0]!.indexOf("dispatchPageBinding !== approvedPageBinding") < programs[0]!.indexOf("return { changed: true };"));
});

test("post-dispatch failures do not replace a stopping state", async () => {
  const { broker, context } = harness();
  context.computer!.exec = async () => {
    context.runState = "stopping";
    return { ok: false, exitCode: 1, stdout: "", stderr: "runner failed", durationMs: 1 };
  };
  await broker.runProgram("return true;", true, "Run once");
  const pending = context.pendingApproval!;

  const outcome = await broker.resolveApproval(pending.approvalId, pending.actionDigest, true);

  assert.deepEqual(outcome, { resumeAgent: false });
  assert.equal(context.runState, "stopping");
  assert.equal(context.recovery, undefined);
  assert.equal(context.operationJournal.at(-1)?.state, "outcome_unknown");
});
