import assert from "node:assert/strict";
import test from "node:test";
import {
  operationProgram,
  operationReason,
  redactBrowserOperation,
  validateBrowserOperation,
  type ExecutableBrowserOperation,
} from "../server/browser-operations.js";

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
    [{ kind: "click", ref: "e1", target: { role: "button", name: "Save", nth: 0 } }, "Click button “Save”"],
    [{ kind: "fill", ref: "e2", target: { role: "textbox", name: "Email", nth: 0 }, value: "person@example.com" }, "Fill textbox “Email”"],
    [{ kind: "select", ref: "e3", target: { role: "combobox", name: "Size", nth: 0 }, label: "Medium" }, "Choose an option in combobox “Size”"],
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
  assert.throws(() => validateBrowserOperation({ kind: "click", ref: "dialog-1" } as never), /ref is invalid/);
  assert.throws(() => validateBrowserOperation({ kind: "click", ref: "e0" }), /ref is invalid/);
  assert.throws(() => validateBrowserOperation({ kind: "fill", ref: "e1", value: "x".repeat(2_001) }), /too long/);
  assert.throws(() => validateBrowserOperation({ kind: "select", ref: "e1", label: "   " }), /option label is invalid/);
  assert.throws(() => validateBrowserOperation({ kind: "unsupported" } as never), /not available/);
});

test("public browser operations redact fill values without changing execution data", () => {
  const operation = { kind: "fill", ref: "e2", target: { role: "textbox", name: "Email", nth: 0 }, value: "person@example.com" } as const;

  assert.deepEqual(redactBrowserOperation(operation), { kind: "fill", ref: "e2" });
  assert.equal(operation.value, "person@example.com");
  assert.deepEqual(redactBrowserOperation({ kind: "click", ref: "e1", target: { role: "button", name: "Save", nth: 0 } }), { kind: "click", ref: "e1" });
});

test("operation programs cover every action with page and field safety checks", () => {
  const expectedPage = "https://example.com/form?q=private#section";
  const programs = [
    operationProgram({ kind: "observe" }),
    operationProgram({ kind: "scroll", direction: "up" }),
    operationProgram({ kind: "navigate", url: "https://example.org/path?q=1" }, expectedPage),
    operationProgram({ kind: "click", ref: "e1", target: { role: "button", name: "Save", nth: 0 } }, expectedPage),
    operationProgram({ kind: "fill", ref: "e2", target: { role: "textbox", name: "Name", nth: 0 }, value: "Ada" }, expectedPage),
    operationProgram({ kind: "select", ref: "e3", target: { role: "combobox", name: "Size", nth: 0 }, label: "Medium" }, expectedPage),
    operationProgram({ kind: "keypress", key: "ArrowDown" }, expectedPage),
  ];

  for (const program of programs) assert.doesNotThrow(() => new Function("page", `return (async () => { ${program} })();`));
  assert.match(programs[0], /ariaSnapshot/);
  assert.match(programs[0], /snapshotRefs\.length < 100/);
  assert.match(programs[0], /textBlocked/);
  assert.match(programs[1], /mouse\.wheel\(0, -600\)/);
  assert.match(programs[2], /currentPage !== "https:\/\/example\.com\/form\?q=private#section"/);
  assert.match(programs[2], /page\.goto\("https:\/\/example\.org\/path\?q=1"\)/);
  assert.match(programs[3], /candidates\.count\(\) <= 0/);
  assert.match(programs[4], /fieldSafety\.type === 'password'/);
  assert.match(programs[4], /target\.fill\("Ada"\)/);
  assert.match(programs[5], /selectOption\(\{ label: "Medium" \}\)/);
  assert.match(programs[6], /keyboard\.press\("ArrowDown"\)/);
});

test("generated programs enforce page, target, observation, and field checks at runtime", async () => {
  const observed = await executeOperationProgram(operationProgram({ kind: "observe" }), {
    url: () => "https://example.com/catalog",
    title: async () => "Catalog",
    locator: () => ({ ariaSnapshot: async () => '- document "Catalog"\n  - text: Email ada@example.com card 4111 1111 1111 1111\n  - button "Buy"' }),
  }) as { snapshot: string; refs: Array<{ ref: string; role: string; name: string }>; textBlocked?: boolean };
  assert.match(observed.snapshot, /Email \[email redacted\] card \[number redacted\]/);
  assert.deepEqual(observed.refs, [{ ref: "e1", role: "document", name: "Catalog", publicName: "Catalog", nth: 0, actionable: false }, { ref: "e2", role: "button", name: "Buy", publicName: "Buy", nth: 0, actionable: true }]);
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
    executeOperationProgram(operationProgram({ kind: "click", ref: "e1", target: { role: "button", name: "Save", nth: 0 } }, "https://example.com/start"), {
      url: () => "https://example.com/start",
      getByRole: () => ({ count: async () => 0, nth: () => ({ click: async () => undefined }) }),
    }),
    /target is no longer available/i,
  );

  let filled = false;
  await assert.rejects(
    executeOperationProgram(operationProgram({ kind: "fill", ref: "e2", target: { role: "textbox", name: "Name", nth: 0 }, value: "Ada" }, "https://example.com/start"), {
      url: () => "https://example.com/start",
      getByRole: () => ({
        count: async () => 1,
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
