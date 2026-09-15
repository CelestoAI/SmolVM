export type BrowserTarget = {
  role: string;
  name: string;
  nth: number;
  publicName?: string;
};

export type BrowserOperation =
  | { kind: "observe" }
  | { kind: "extract"; scopeRef?: string }
  | { kind: "scroll"; direction: "up" | "down" }
  | { kind: "navigate"; url: string }
  | { kind: "click"; ref: string }
  | { kind: "fill"; ref: string; value: string }
  | { kind: "select"; ref: string; label: string }
  | { kind: "keypress"; key: "Enter" | "Escape" | "Tab" | "ArrowUp" | "ArrowDown" | "ArrowLeft" | "ArrowRight" };

export type ExecutableBrowserOperation =
  | Exclude<BrowserOperation, { kind: "click" | "fill" | "select" | "extract" }>
  | { kind: "extract"; scopeRef?: string; target?: BrowserTarget }
  | { kind: "click"; ref: string; target: BrowserTarget }
  | { kind: "fill"; ref: string; target: BrowserTarget; value: string }
  | { kind: "select"; ref: string; target: BrowserTarget; label: string };

export type PublicBrowserOperation = Exclude<ExecutableBrowserOperation, { kind: "click" | "fill" | "select" }>
  | { kind: "click"; ref: string }
  | { kind: "fill"; ref: string }
  | { kind: "select"; ref: string; label: string };

const ALLOWED_KEYS = new Set(["Enter", "Escape", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
const REF_PATTERN = /^e[1-9]\d{0,2}$/;

export function validateBrowserOperation(operation: BrowserOperation): BrowserOperation {
  if (!operation || typeof operation !== "object" || typeof operation.kind !== "string") throw new Error("The browser operation is invalid.");
  if (operation.kind === "observe") return operation;
  if (operation.kind === "extract") {
    if (operation.scopeRef !== undefined && !validRef(operation.scopeRef)) throw new Error("The browser ref is invalid. Observe the page again and use a current ref.");
    return operation.scopeRef === undefined ? operation : { kind: "extract", scopeRef: operation.scopeRef.trim() };
  }
  if (operation.kind === "scroll") {
    if (operation.direction !== "up" && operation.direction !== "down") throw new Error("The scroll direction must be up or down.");
    return operation;
  }
  if (operation.kind === "navigate") {
    if (typeof operation.url !== "string" || operation.url.length > 2_048) throw new Error("The website address is invalid.");
    let url: URL;
    try { url = new URL(operation.url); } catch { throw new Error("The website address is invalid."); }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("OpenMuse can navigate only to ordinary public HTTP or HTTPS addresses.");
    const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.+$/, "");
    if (isPrivateHostname(hostname)) throw new Error("OpenMuse cannot navigate to a private or local network address.");
    return { kind: "navigate", url: url.href };
  }
  if (operation.kind === "keypress") {
    if (!ALLOWED_KEYS.has(operation.key)) throw new Error("That browser key is not available.");
    return operation;
  }
  if (operation.kind !== "click" && operation.kind !== "fill" && operation.kind !== "select") throw new Error("That browser operation is not available.");
  if (!validRef(operation.ref)) throw new Error("The browser ref is invalid. Observe the page again and use a current ref.");
  const ref = operation.ref.trim();
  if (operation.kind === "fill") {
    if (typeof operation.value !== "string" || operation.value.length > 2_000) throw new Error("The field value is too long.");
    return { ...operation, ref };
  }
  if (operation.kind === "select") {
    if (typeof operation.label !== "string" || !operation.label.trim() || operation.label.length > 160) throw new Error("The option label is invalid.");
    return { ...operation, ref, label: operation.label.trim() };
  }
  return { ...operation, ref };
}

export function redactBrowserOperation(operation?: ExecutableBrowserOperation): PublicBrowserOperation | undefined {
  if (operation?.kind === "click" || operation?.kind === "fill") return { kind: operation.kind, ref: operation.ref };
  if (operation?.kind === "select") return { kind: operation.kind, ref: operation.ref, label: operation.label };
  return operation;
}

export function operationReason(operation: ExecutableBrowserOperation): string {
  switch (operation.kind) {
    case "observe": return "Read the current page";
    case "extract": return operation.target ? `Extract ${operation.target.role} “${operation.target.publicName ?? operation.target.name}”` : "Extract the current page";
    case "scroll": return `Scroll ${operation.direction}`;
    case "navigate": return `Open ${operation.url}`;
    case "click": return `Click ${operation.target.role} “${operation.target.publicName ?? operation.target.name}”`;
    case "fill": return `Fill ${operation.target.role} “${operation.target.publicName ?? operation.target.name}”`;
    case "select": return `Choose an option in ${operation.target.role} “${operation.target.publicName ?? operation.target.name}”`;
    case "keypress": return `Press ${operation.key}`;
  }
}

export function operationProgram(operation: ExecutableBrowserOperation, expectedPage?: string): string {
  const binding = expectedPage === undefined ? [] : [
    "const currentRawUrl = page.url();",
    "const currentParsedUrl = (() => { try { return new URL(currentRawUrl); } catch { return null; } })();",
    "const currentPage = currentParsedUrl && ['http:', 'https:'].includes(currentParsedUrl.protocol) ? `${currentParsedUrl.origin}${currentParsedUrl.pathname}${currentParsedUrl.search}${currentParsedUrl.hash}` : currentRawUrl;",
    `if (currentPage !== ${JSON.stringify(expectedPage)}) throw new Error('The page changed after approval.');`,
  ];
  switch (operation.kind) {
    case "observe": return snapshotProgram();
    case "extract": return extractionProgram(operation.target);
    case "scroll": return [`await page.mouse.wheel(0, ${operation.direction === "down" ? 600 : -600});`, snapshotProgram(`{ scrolled: ${JSON.stringify(operation.direction)}, observation }`)].join("\n");
    case "navigate": return [...binding, `await page.goto(${JSON.stringify(operation.url)});`, snapshotProgram(`{ opened: ${JSON.stringify(operation.url)}, observation }`)].join("\n");
    case "click": return [...binding, targetProgram(operation.target), "await target.click(); return { clicked: true };"].join("\n");
    case "fill": return [
      ...binding, targetProgram(operation.target),
      "const fieldSafety = await target.evaluate((node) => ({ type: (node.getAttribute('type') || '').toLowerCase(), autocomplete: (node.getAttribute('autocomplete') || '').toLowerCase() }));",
      "if (fieldSafety.type === 'password' || /(?:^|\\s)(?:cc-|current-password|new-password|one-time-code)/.test(fieldSafety.autocomplete)) throw new Error('Use Take control to enter passwords, payment details, codes, or other secrets.');",
      `await target.fill(${JSON.stringify(operation.value)}); return { filled: true };`,
    ].join("\n");
    case "select": return [...binding, targetProgram(operation.target), `await target.selectOption({ label: ${JSON.stringify(operation.label)} }); return { selected: ${JSON.stringify(operation.label)} };`].join("\n");
    case "keypress": return [...binding, `await page.keyboard.press(${JSON.stringify(operation.key)}); return { pressed: ${JSON.stringify(operation.key)} };`].join("\n");
  }
}

function snapshotProgram(resultExpression = "observation"): string {
  return [
    "const snapshotRawUrl = page.url();",
    "const snapshotParsedUrl = (() => { try { return new URL(snapshotRawUrl); } catch { return null; } })();",
    "const snapshotSafeUrl = snapshotParsedUrl && ['http:', 'https:'].includes(snapshotParsedUrl.protocol) ? `${snapshotParsedUrl.origin}${snapshotParsedUrl.pathname}` : snapshotRawUrl;",
    "const snapshotPageBinding = snapshotParsedUrl && ['http:', 'https:'].includes(snapshotParsedUrl.protocol) ? `${snapshotParsedUrl.origin}${snapshotParsedUrl.pathname}${snapshotParsedUrl.search}${snapshotParsedUrl.hash}` : snapshotRawUrl;",
    "const snapshotSensitivePath = snapshotParsedUrl ? /(?:^|\\/)(?:account|auth|billing|checkout|login|orders?|payments?|profile|signin|wallet)(?:\\/|$)/i.test(snapshotParsedUrl.pathname) : true;",
    "const snapshotRaw = snapshotSensitivePath ? '' : await page.locator('body').ariaSnapshot().catch(() => '');",
    "const snapshotRedact = (value) => value.replace(/\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b/gi, '[email redacted]').replace(/\\b(?:\\d[ -]*?){13,19}\\b/g, '[number redacted]');",
    "const snapshotActionRoles = new Set(['button', 'checkbox', 'combobox', 'link', 'menuitem', 'radio', 'searchbox', 'spinbutton', 'textbox']);",
    "const snapshotCounts = new Map();",
    "const snapshotRefs = [];",
    "const snapshotLines = [];",
    "let snapshotLength = 0;",
    "for (const line of snapshotRaw.split('\\n')) {",
    "  const match = line.match(/^(\\s*-\\s+)([a-z][a-z0-9]*)\\s+\"((?:[^\"\\\\]|\\\\.)*)\"(.*)$/);",
    "  let rendered = snapshotRedact(line);",
    "  let candidate;",
    "  if (match && snapshotRefs.length < 100) {",
    "    let name; try { name = JSON.parse(`\"${match[3]}\"`); } catch { name = match[3]; }",
    "    const key = `${match[2]}\\u0000${name}`;",
    "    const nth = snapshotCounts.get(key) || 0; snapshotCounts.set(key, nth + 1);",
    "    candidate = { ref: `e${snapshotRefs.length + 1}`, role: match[2], name, publicName: snapshotRedact(name).slice(0, 160), nth, actionable: snapshotActionRoles.has(match[2]) };",
    "    rendered = `${match[1]}${match[2]} \"${candidate.publicName}\" [ref=${candidate.ref}]${match[4]}`;",
    "  }",
    "  const addition = `${rendered}\\n`;",
    "  if (snapshotLength + addition.length > 12000) break;",
    "  snapshotLines.push(rendered); snapshotLength += addition.length; if (candidate) snapshotRefs.push(candidate);",
    "}",
    "const observation = { title: await page.title().catch(() => ''), url: snapshotSafeUrl, pageBinding: snapshotPageBinding, snapshot: snapshotLines.join('\\n'), refs: snapshotRefs, truncated: snapshotLength < snapshotRaw.length, ...(snapshotSensitivePath ? { textBlocked: true } : {}) };",
    `return ${resultExpression};`,
  ].join("\n");
}

function extractionProgram(target?: BrowserTarget): string {
  return [
    "const extractRawUrl = page.url();",
    "const extractParsedUrl = (() => { try { return new URL(extractRawUrl); } catch { return null; } })();",
    "const extractSafeUrl = extractParsedUrl && ['http:', 'https:'].includes(extractParsedUrl.protocol) ? `${extractParsedUrl.origin}${extractParsedUrl.pathname}` : extractRawUrl;",
    "const extractPageBinding = extractParsedUrl && ['http:', 'https:'].includes(extractParsedUrl.protocol) ? `${extractParsedUrl.origin}${extractParsedUrl.pathname}${extractParsedUrl.search}${extractParsedUrl.hash}` : extractRawUrl;",
    "const extractSensitivePath = extractParsedUrl ? /(?:^|\\/)(?:account|auth|billing|checkout|login|orders?|payments?|profile|signin|wallet)(?:\\/|$)/i.test(extractParsedUrl.pathname) : true;",
    ...(target ? [targetProgram(target)] : ["const target = page.locator('body');"]),
    "const extractRawText = extractSensitivePath ? '' : await target.innerText({ timeout: 5_000 }).catch(() => '');",
    "const extractText = extractRawText.replace(/\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b/gi, '[email redacted]').replace(/\\b(?:\\d[ -]*?){13,19}\\b/g, '[number redacted]').slice(0, 16000);",
    "return { title: await page.title().catch(() => ''), url: extractSafeUrl, pageBinding: extractPageBinding, text: extractText, truncated: extractRawText.length > 16000, ...(extractSensitivePath ? { textBlocked: true } : {}) };",
  ].join("\n");
}

function validRef(ref: unknown): ref is string { return typeof ref === "string" && REF_PATTERN.test(ref.trim()); }

function isPrivateHostname(hostname: string): boolean {
  if (hostname === "localhost" || hostname.endsWith(".localhost") || hostname === "::1" || hostname === "0.0.0.0") return true;
  if (hostname.startsWith("::ffff:")) return true;
  const octets = hostname.split(".").map(Number);
  if (octets.length === 4 && octets.every((part) => Number.isInteger(part) && part >= 0 && part <= 255)) {
    return octets[0] === 0 || octets[0] === 10 || octets[0] === 127 || octets[0] >= 224
      || octets[0] === 100 && octets[1] >= 64 && octets[1] <= 127
      || octets[0] === 169 && octets[1] === 254 || octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31
      || octets[0] === 192 && (octets[1] === 168 || octets[1] === 0 && [0, 2].includes(octets[2]))
      || octets[0] === 198 && (octets[1] === 18 || octets[1] === 19 || octets[1] === 51 && octets[2] === 100)
      || octets[0] === 203 && octets[1] === 0 && octets[2] === 113;
  }
  return /^(?:::|fc|fd|fe8|fe9|fea|feb|ff|2001:db8)/i.test(hostname);
}

function targetProgram(target: BrowserTarget): string {
  return [
    `const candidates = page.getByRole(${JSON.stringify(target.role)}, { name: ${JSON.stringify(target.name)}, exact: true });`,
    `if (await candidates.count() <= ${target.nth}) throw new Error('The observed target is no longer available.');`,
    `const target = candidates.nth(${target.nth});`,
  ].join("\n");
}
