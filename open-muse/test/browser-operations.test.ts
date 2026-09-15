import assert from "node:assert/strict";
import test from "node:test";
import {
  operationReason,
  redactBrowserOperation,
  validateBrowserOperation,
  type ExecutableBrowserOperation,
} from "../server/browser-operations.js";
import { BrowserDriverError, executeBrowserOperation } from "../server/browser-driver.js";
import type { Page } from "playwright-core";

const LOCATOR_ID = "00000000-0000-4000-8000-000000000001";

test("operation reasons describe every supported browser action", () => {
  const cases: Array<[ExecutableBrowserOperation, string]> = [
    [{ kind: "observe" }, "Read the current page"],
    [{ kind: "extract" }, "Extract the current page"],
    [{ kind: "scroll", direction: "up" }, "Scroll up"],
    [{ kind: "navigate", url: "https://example.com" }, "Open https://example.com"],
    [{ kind: "click", ref: "e1", target: { role: "button", name: "Save", nth: 0, locatorId: LOCATOR_ID } }, "Click button “Save”"],
    [{ kind: "fill", ref: "e2", target: { role: "textbox", name: "Email", nth: 0, locatorId: LOCATOR_ID }, value: "person@example.com" }, "Fill textbox “Email”"],
    [{ kind: "select", ref: "e3", target: { role: "combobox", name: "Size", nth: 0, locatorId: LOCATOR_ID }, label: "Medium" }, "Choose an option in combobox “Size”"],
    [{ kind: "keypress", key: "Enter" }, "Press Enter"],
  ];

  for (const [operation, expected] of cases) assert.equal(operationReason(operation), expected);
});

test("operation validation normalizes public URLs and refs", () => {
  assert.deepEqual(
    validateBrowserOperation({ kind: "navigate", url: "HTTPS://Example.COM/path?q=1#result" }),
    { kind: "navigate", url: "https://example.com/path?q=1#result" },
  );
  assert.deepEqual(
    validateBrowserOperation({ kind: "click", ref: " e1 " }),
    { kind: "click", ref: "e1" },
  );
  assert.deepEqual(
    validateBrowserOperation({ kind: "fill", ref: "e2", value: "muse" }),
    { kind: "fill", ref: "e2", value: "muse" },
  );
  assert.deepEqual(
    validateBrowserOperation({ kind: "select", ref: "e3", label: "  Medium  " }),
    { kind: "select", ref: "e3", label: "Medium" },
  );
  assert.deepEqual(validateBrowserOperation({ kind: "extract", scopeRef: " e4 " }), { kind: "extract", scopeRef: "e4" });
  assert.deepEqual(validateBrowserOperation({ kind: "scroll", direction: "down" }), { kind: "scroll", direction: "down" });
  assert.deepEqual(validateBrowserOperation({ kind: "keypress", key: "Escape" }), { kind: "keypress", key: "Escape" });
});

test("operation validation rejects malformed, local, and sensitive actions", () => {
  for (const url of [
    "http://localhost/admin",
    "http://localhost./admin",
    "http://service.localhost/admin",
    "http://service.localhost./admin",
    "http://0.0.0.1/admin",
    "http://10.0.0.1/admin",
    "http://100.64.0.1/admin",
    "http://127.0.0.1/admin",
    "http://169.254.1.1/admin",
    "http://172.16.0.1/admin",
    "http://192.168.0.1/admin",
    "http://192.0.2.1/admin",
    "http://198.18.0.1/admin",
    "http://198.51.100.1/admin",
    "http://203.0.113.1/admin",
    "http://224.0.0.1/admin",
    "http://[::1]/admin",
    "http://[fc00::1]/admin",
    "http://[fe80::1]/admin",
    "http://[ff02::1]/admin",
    "http://[2001:db8::1]/admin",
    "http://[::ffff:192.168.0.1]/admin",
  ]) {
    assert.throws(
      () => validateBrowserOperation({ kind: "navigate", url }),
      /private or local/,
      url,
    );
  }

  assert.throws(() => validateBrowserOperation(null as never), /operation is invalid/);
  assert.throws(() => validateBrowserOperation({ kind: "scroll", direction: "sideways" } as never), /up or down/);
  assert.throws(() => validateBrowserOperation({ kind: "navigate", url: "not a url" }), /address is invalid/);
  const credentialedUrl = new URL("https://example.com");
  credentialedUrl.username = "test-user";
  assert.throws(() => validateBrowserOperation({ kind: "navigate", url: credentialedUrl.href }), /ordinary public HTTP or HTTPS/);
  assert.throws(() => validateBrowserOperation({ kind: "keypress", key: "Meta+A" } as never), /key is not available/);
  assert.throws(() => validateBrowserOperation({ kind: "extract", scopeRef: "section-1" }), /ref is invalid/);
  assert.throws(() => validateBrowserOperation({ kind: "click", ref: "dialog-1" } as never), /ref is invalid/);
  assert.throws(() => validateBrowserOperation({ kind: "click", ref: "e0" }), /ref is invalid/);
  assert.throws(() => validateBrowserOperation({ kind: "fill", ref: "e1", value: "x".repeat(2_001) }), /too long/);
  assert.throws(() => validateBrowserOperation({ kind: "select", ref: "e1", label: "   " }), /option label is invalid/);
  assert.throws(() => validateBrowserOperation({ kind: "unsupported" } as never), /not available/);
});

test("public browser operations redact fill values without changing execution data", () => {
  const operation = { kind: "fill", ref: "e2", target: { role: "textbox", name: "Email", nth: 0, locatorId: LOCATOR_ID }, value: "person@example.com" } as const;

  assert.deepEqual(redactBrowserOperation(operation), { kind: "fill", ref: "e2" });
  assert.equal(operation.value, "person@example.com");
  assert.deepEqual(redactBrowserOperation({ kind: "click", ref: "e1", target: { role: "button", name: "Save", nth: 0, locatorId: LOCATOR_ID } }), { kind: "click", ref: "e1" });
});

function page(value: Record<string, unknown>): Page {
  return {
    isClosed: () => false,
    context: () => ({ browser: () => ({ isConnected: () => true }) }),
    ...value,
  } as unknown as Page;
}

test("host driver observes, redacts, and binds semantic refs", async () => {
  const observed = await executeBrowserOperation(page({
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ ariaSnapshot: async () => '- document "Catalog"\n  - text: Email ada@example.com card 4111 1111 1111 1111\n  - button "Buy"' }),
    getByRole: () => ({ nth: () => ({ evaluate: async () => undefined }) }),
  }), { kind: "observe" }) as { snapshot: string; refs: Array<{ ref: string; role: string; name: string }>; textBlocked?: boolean };
  assert.match(observed.snapshot, /Email \[email redacted\] card \[number redacted\]/);
  assert.deepEqual(observed.refs.map(({ locatorId: _locatorId, ...ref }) => ref), [{ ref: "e1", role: "document", name: "Catalog", publicName: "Catalog", nth: 0, actionable: false }, { ref: "e2", role: "button", name: "Buy", publicName: "Buy", nth: 0, actionable: true }]);
  assert.equal(new Set(observed.refs.map((ref) => (ref as { locatorId: string }).locatorId)).size, 2);
  for (const ref of observed.refs) assert.match((ref as { locatorId: string }).locatorId, /^[0-9a-f-]{36}$/i);
  assert.equal(observed.textBlocked, undefined);

  const sensitive = await executeBrowserOperation(page({
    url: () => "https://example.com/checkout",
    title: async () => "Checkout",
    locator: () => ({ ariaSnapshot: async () => { throw new Error("must not read"); } }),
  }), { kind: "observe" }) as { snapshot: string; refs: unknown[]; textBlocked?: boolean };
  assert.equal(sensitive.snapshot, "");
  assert.deepEqual(sensitive.refs, []);
  assert.equal(sensitive.textBlocked, true);
});

test("host driver revalidates the page, target, and sensitive fields", async () => {
  let navigated = false;
  await assert.rejects(
    executeBrowserOperation(page({
      url: () => "https://example.com/changed",
      goto: async () => { navigated = true; },
    }), { kind: "navigate", url: "https://example.org" }, "https://example.com/start"),
    /approved page changed before execution/i,
  );
  assert.equal(navigated, false);

  await assert.rejects(
    executeBrowserOperation(page({
      url: () => "https://example.com/start",
      locator: () => ({ and: () => ({ count: async () => 0, first: () => ({ click: async () => undefined }) }) }),
      getByRole: () => ({}),
    }), { kind: "click", ref: "e1", target: { role: "button", name: "Save", nth: 0, locatorId: LOCATOR_ID } }, "https://example.com/start"),
    /target is no longer available/i,
  );

  let filled = false;
  await assert.rejects(
    executeBrowserOperation(page({
      url: () => "https://example.com/start",
      locator: () => ({
        and: () => ({
          count: async () => 1,
          first: () => ({
            evaluate: async () => ({ type: "password", autocomplete: "" }),
            fill: async () => { filled = true; },
          }),
        }),
      }),
      getByRole: () => ({
        nth: () => ({
          evaluate: async () => ({ type: "password", autocomplete: "" }),
          fill: async () => { filled = true; },
        }),
      }),
    }), { kind: "fill", ref: "e2", target: { role: "textbox", name: "Name", nth: 0, locatorId: LOCATOR_ID }, value: "Ada" }, "https://example.com/start"),
    /Take control/,
  );
  assert.equal(filled, false);
});

test("host driver returns typed failures and accurate truncation", async () => {
  await assert.rejects(
    executeBrowserOperation(page({
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ ariaSnapshot: async () => { throw new Error("snapshot unavailable"); } }),
    }), { kind: "observe" }),
    (error: unknown) => error instanceof BrowserDriverError && error.code === "ACCESSIBILITY_CAPTURE_FAILED",
  );

  const completeRedactedObservation = await executeBrowserOperation(page({
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ ariaSnapshot: async () => "- text: card 4111 1111 1111 1111" }),
    getByRole: () => { throw new Error("text nodes do not receive refs"); },
  }), { kind: "observe" }) as { truncated: boolean; snapshot: string };
  assert.equal(completeRedactedObservation.truncated, false);
  assert.match(completeRedactedObservation.snapshot, /\[number redacted\]/);

  const truncatedObservation = await executeBrowserOperation(page({
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ ariaSnapshot: async () => `- text: ${"x".repeat(12_001)}` }),
  }), { kind: "observe" }) as { truncated: boolean; snapshot: string };
  assert.equal(truncatedObservation.truncated, true);
  assert.equal(truncatedObservation.snapshot, "");

  const truncatedExtraction = await executeBrowserOperation(page({
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ innerText: async () => "x".repeat(16_001) }),
  }), { kind: "extract" }) as { text: string; truncated: boolean };
  assert.equal(truncatedExtraction.text.length, 16_000);
  assert.equal(truncatedExtraction.truncated, true);

  const sensitiveExtraction = await executeBrowserOperation(page({
    url: () => "https://example.com/account",
    title: async () => "Account",
    locator: () => ({ innerText: async () => { throw new Error("must not read"); } }),
  }), { kind: "extract" }) as { text: string; textBlocked?: boolean };
  assert.equal(sensitiveExtraction.text, "");
  assert.equal(sensitiveExtraction.textBlocked, true);
});

test("marker-bound actions do not rebind by ordinal position", async () => {
  let clicked = false;
  await executeBrowserOperation(page({
    url: () => "https://example.com/catalog",
    locator: () => ({
      and: () => ({
        count: async () => 1,
        first: () => ({ click: async () => { clicked = true; } }),
      }),
    }),
    getByRole: () => ({}),
  }), {
    kind: "click",
    ref: "e2",
    target: { role: "button", name: "Add to cart", nth: 99, locatorId: LOCATOR_ID },
  }, "https://example.com/catalog");
  assert.equal(clicked, true);
});

test("host driver executes every structured interaction directly on the page", async () => {
  const calls: string[] = [];
  const target = {
    count: async () => 1,
    first: () => ({
      click: async () => { calls.push("click"); },
      evaluate: async () => ({ type: "text", autocomplete: "" }),
      fill: async (value: string) => { calls.push(`fill:${value}`); },
      selectOption: async ({ label }: { label: string }) => { calls.push(`select:${label}`); },
    }),
  };
  const hostPage = page({
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    goto: async (url: string) => { calls.push(`goto:${url}`); },
    mouse: { wheel: async (_x: number, y: number) => { calls.push(`wheel:${y}`); } },
    keyboard: { press: async (key: string) => { calls.push(`key:${key}`); } },
    locator: (selector: string) => selector === "body"
      ? { ariaSnapshot: async () => "- text: Catalog", innerText: async () => "Catalog" }
      : { and: () => target },
    getByRole: () => ({}),
  });
  const markedTarget = { role: "button", name: "Save", nth: 0, locatorId: LOCATOR_ID };

  await executeBrowserOperation(hostPage, { kind: "scroll", direction: "down" });
  await executeBrowserOperation(hostPage, { kind: "navigate", url: "https://example.org" });
  await executeBrowserOperation(hostPage, { kind: "click", ref: "e1", target: markedTarget });
  await executeBrowserOperation(hostPage, { kind: "fill", ref: "e1", target: markedTarget, value: "Ada" });
  await executeBrowserOperation(hostPage, { kind: "select", ref: "e1", target: markedTarget, label: "Medium" });
  await executeBrowserOperation(hostPage, { kind: "keypress", key: "Enter" });

  assert.deepEqual(calls, ["wheel:600", "goto:https://example.org", "click", "fill:Ada", "select:Medium", "key:Enter"]);
});

test("host driver distinguishes a lost CDP connection from a visible page", async () => {
  const disconnected = page({ context: () => ({ browser: () => ({ isConnected: () => false }) }) });

  await assert.rejects(
    executeBrowserOperation(disconnected, { kind: "keypress", key: "Enter" }),
    (error: unknown) => error instanceof BrowserDriverError
      && error.code === "CDP_DISCONNECTED"
      && /automation connection was lost/.test(error.message),
  );

  await assert.rejects(
    executeBrowserOperation(page({
      url: () => "https://example.com/catalog",
      locator: () => ({ ariaSnapshot: async () => { throw new Error("locator timed out after 10000ms"); } }),
    }), { kind: "observe" }),
    (error: unknown) => error instanceof BrowserDriverError && error.code === "OPERATION_TIMEOUT",
  );
});
