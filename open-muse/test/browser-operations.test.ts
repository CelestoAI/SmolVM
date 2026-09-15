import assert from "node:assert/strict";
import test from "node:test";
import {
  operationProgram,
  operationReason,
  redactBrowserOperation,
  validateBrowserOperation,
  type ExecutableBrowserOperation,
} from "../server/browser-operations.js";

const LOCATOR_ID = "00000000-0000-4000-8000-000000000001";

async function executeOperationProgram(program: string, page: unknown): Promise<unknown> {
  const execute = new Function("page", `return (async () => { ${program} })();`) as (value: unknown) => Promise<unknown>;
  return execute(page);
}

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

test("operation programs cover every action with page and field safety checks", () => {
  const expectedPage = "https://example.com/form?q=private#section";
  const programs = [
    operationProgram({ kind: "observe" }),
    operationProgram({ kind: "extract", target: { role: "region", name: "Products", nth: 0, locatorId: LOCATOR_ID } }),
    operationProgram({ kind: "scroll", direction: "up" }),
    operationProgram({ kind: "navigate", url: "https://example.org/path?q=1" }, expectedPage),
    operationProgram({ kind: "click", ref: "e1", target: { role: "button", name: "Save", nth: 0, locatorId: LOCATOR_ID } }, expectedPage),
    operationProgram({ kind: "fill", ref: "e2", target: { role: "textbox", name: "Name", nth: 0, locatorId: LOCATOR_ID }, value: "Ada" }, expectedPage),
    operationProgram({ kind: "select", ref: "e3", target: { role: "combobox", name: "Size", nth: 0, locatorId: LOCATOR_ID }, label: "Medium" }, expectedPage),
    operationProgram({ kind: "keypress", key: "ArrowDown" }, expectedPage),
  ];

  for (const program of programs) assert.doesNotThrow(() => new Function("page", `return (async () => { ${program} })();`));
  assert.match(programs[0], /ariaSnapshot/);
  assert.match(programs[0], /snapshotRefs\.length < 100/);
  assert.match(programs[0], /textBlocked/);
  assert.match(programs[1], /data-smolvm-browser-ref/);
  assert.match(programs[1], /target\.innerText/);
  assert.match(programs[2], /mouse\.wheel\(0, -600\)/);
  assert.match(programs[3], /currentPage !== "https:\/\/example\.com\/form\?q=private#section"/);
  assert.match(programs[3], /page\.goto\("https:\/\/example\.org\/path\?q=1"\)/);
  assert.match(programs[4], /data-smolvm-browser-ref/);
  assert.match(programs[4], /candidates\.count\(\) !== 1/);
  assert.match(programs[5], /fieldSafety\.type === 'password'/);
  assert.match(programs[5], /target\.fill\("Ada"\)/);
  assert.match(programs[6], /selectOption\(\{ label: "Medium" \}\)/);
  assert.match(programs[7], /keyboard\.press\("ArrowDown"\)/);
});

test("generated programs enforce page, target, observation, and field checks at runtime", async () => {
  const observed = await executeOperationProgram(operationProgram({ kind: "observe" }), {
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ ariaSnapshot: async () => '- document "Catalog"\n  - text: Email ada@example.com card 4111 1111 1111 1111\n  - button "Buy"' }),
    getByRole: () => ({ nth: () => ({ evaluate: async () => LOCATOR_ID }) }),
  }) as { snapshot: string; refs: Array<{ ref: string; role: string; name: string }>; textBlocked?: boolean };
  assert.match(observed.snapshot, /Email \[email redacted\] card \[number redacted\]/);
  assert.deepEqual(observed.refs.map(({ locatorId: _locatorId, ...ref }) => ref), [{ ref: "e1", role: "document", name: "Catalog", publicName: "Catalog", nth: 0, actionable: false }, { ref: "e2", role: "button", name: "Buy", publicName: "Buy", nth: 0, actionable: true }]);
  assert.equal(new Set(observed.refs.map((ref) => (ref as { locatorId: string }).locatorId)).size, 2);
  for (const ref of observed.refs) assert.match((ref as { locatorId: string }).locatorId, /^[0-9a-f-]{36}$/i);
  assert.equal(observed.textBlocked, undefined);

  const sensitive = await executeOperationProgram(operationProgram({ kind: "observe" }), {
    url: () => "https://example.com/checkout",
    title: async () => "Checkout",
    locator: () => ({ ariaSnapshot: async () => { throw new Error("must not read"); } }),
  }) as { snapshot: string; refs: unknown[]; textBlocked?: boolean };
  assert.equal(sensitive.snapshot, "");
  assert.deepEqual(sensitive.refs, []);
  assert.equal(sensitive.textBlocked, true);

  let navigated = false;
  await assert.rejects(
    executeOperationProgram(operationProgram({ kind: "navigate", url: "https://example.org" }, "https://example.com/start"), {
      url: () => "https://example.com/changed",
      goto: async () => { navigated = true; },
    }),
    /page changed after approval/i,
  );
  assert.equal(navigated, false);

  await assert.rejects(
    executeOperationProgram(operationProgram({ kind: "click", ref: "e1", target: { role: "button", name: "Save", nth: 0, locatorId: LOCATOR_ID } }, "https://example.com/start"), {
      url: () => "https://example.com/start",
      locator: () => ({ and: () => ({ count: async () => 0, first: () => ({ click: async () => undefined }) }) }),
      getByRole: () => ({}),
    }),
    /target is no longer available/i,
  );

  let filled = false;
  await assert.rejects(
    executeOperationProgram(operationProgram({ kind: "fill", ref: "e2", target: { role: "textbox", name: "Name", nth: 0, locatorId: LOCATOR_ID }, value: "Ada" }, "https://example.com/start"), {
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
    }),
    /Take control/,
  );
  assert.equal(filled, false);
});

test("page capture reports Playwright failures and accurate truncation", async () => {
  const failedObservation = await executeOperationProgram(operationProgram({ kind: "observe" }), {
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ ariaSnapshot: async () => { throw new Error("snapshot timeout"); } }),
  }) as { captureFailed?: boolean; snapshot: string };
  assert.equal(failedObservation.captureFailed, true);
  assert.equal(failedObservation.snapshot, "");

  const completeRedactedObservation = await executeOperationProgram(operationProgram({ kind: "observe" }), {
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ ariaSnapshot: async () => "- text: card 4111 1111 1111 1111" }),
    getByRole: () => { throw new Error("text nodes do not receive refs"); },
  }) as { truncated: boolean; snapshot: string };
  assert.equal(completeRedactedObservation.truncated, false);
  assert.match(completeRedactedObservation.snapshot, /\[number redacted\]/);

  const truncatedObservation = await executeOperationProgram(operationProgram({ kind: "observe" }), {
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ ariaSnapshot: async () => `- text: ${"x".repeat(12_001)}` }),
  }) as { truncated: boolean; snapshot: string };
  assert.equal(truncatedObservation.truncated, true);
  assert.equal(truncatedObservation.snapshot, "");

  const failedExtraction = await executeOperationProgram(operationProgram({ kind: "extract" }), {
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ innerText: async () => { throw new Error("text timeout"); } }),
  }) as { captureFailed?: boolean; text: string };
  assert.equal(failedExtraction.captureFailed, true);
  assert.equal(failedExtraction.text, "");

  const truncatedExtraction = await executeOperationProgram(operationProgram({ kind: "extract" }), {
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ innerText: async () => "x".repeat(16_001) }),
  }) as { text: string; truncated: boolean };
  assert.equal(truncatedExtraction.text.length, 16_000);
  assert.equal(truncatedExtraction.truncated, true);

  const sensitiveExtraction = await executeOperationProgram(operationProgram({ kind: "extract" }), {
    url: () => "https://example.com/account",
    title: async () => "Account",
    locator: () => ({ innerText: async () => { throw new Error("must not read"); } }),
  }) as { text: string; textBlocked?: boolean; captureFailed?: boolean };
  assert.equal(sensitiveExtraction.text, "");
  assert.equal(sensitiveExtraction.textBlocked, true);
  assert.equal(sensitiveExtraction.captureFailed, undefined);

  const sensitiveFilenameObservation = await executeOperationProgram(operationProgram({ kind: "observe" }), {
    url: () => "https://example.com/profile.html",
    title: async () => "Profile",
    locator: () => ({ ariaSnapshot: async () => { throw new Error("must not read"); } }),
  }) as { snapshot: string; textBlocked?: boolean; captureFailed?: boolean };
  assert.equal(sensitiveFilenameObservation.snapshot, "");
  assert.equal(sensitiveFilenameObservation.textBlocked, true);
  assert.equal(sensitiveFilenameObservation.captureFailed, undefined);

  const sensitiveSuffixExtraction = await executeOperationProgram(operationProgram({ kind: "extract" }), {
    url: () => "https://example.com/account-settings",
    title: async () => "Account settings",
    locator: () => ({ innerText: async () => { throw new Error("must not read"); } }),
  }) as { text: string; textBlocked?: boolean; captureFailed?: boolean };
  assert.equal(sensitiveSuffixExtraction.text, "");
  assert.equal(sensitiveSuffixExtraction.textBlocked, true);
  assert.equal(sensitiveSuffixExtraction.captureFailed, undefined);
});

test("marker-bound actions do not rebind by ordinal position", async () => {
  let clicked = false;
  await executeOperationProgram(operationProgram({
    kind: "click",
    ref: "e2",
    target: { role: "button", name: "Add to cart", nth: 99, locatorId: LOCATOR_ID },
  }, "https://example.com/catalog"), {
    url: () => "https://example.com/catalog",
    locator: () => ({
      and: () => ({
        count: async () => 1,
        first: () => ({ click: async () => { clicked = true; } }),
      }),
    }),
    getByRole: () => ({}),
  });
  assert.equal(clicked, true);
});
