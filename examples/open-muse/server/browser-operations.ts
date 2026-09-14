export type BrowserTarget = {
  role: "button" | "checkbox" | "combobox" | "link" | "menuitem" | "radio" | "searchbox" | "spinbutton" | "textbox";
  name: string;
};

export type BrowserOperation =
  | { kind: "observe" }
  | { kind: "scroll"; direction: "up" | "down" }
  | { kind: "navigate"; url: string }
  | { kind: "click"; target: BrowserTarget }
  | { kind: "fill"; target: BrowserTarget; value: string }
  | { kind: "select"; target: BrowserTarget; label: string }
  | { kind: "keypress"; key: "Enter" | "Escape" | "Tab" | "ArrowUp" | "ArrowDown" | "ArrowLeft" | "ArrowRight" };

const SENSITIVE_TARGET = /\b(?:card|credential|cvc|cvv|otp|passcode|password|payment|secret|security code|token)\b/i;
const ALLOWED_KEYS = new Set(["Enter", "Escape", "Tab", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
const ALLOWED_ROLES = new Set(["button", "checkbox", "combobox", "link", "menuitem", "radio", "searchbox", "spinbutton", "textbox"]);

export function validateBrowserOperation(operation: BrowserOperation): BrowserOperation {
  if (!operation || typeof operation !== "object" || typeof operation.kind !== "string") throw new Error("The browser operation is invalid.");
  if (operation.kind === "observe") return operation;
  if (operation.kind === "scroll") {
    if (operation.direction !== "up" && operation.direction !== "down") throw new Error("The scroll direction must be up or down.");
    return operation;
  }
  if (operation.kind === "navigate") {
    if (typeof operation.url !== "string" || operation.url.length > 2_048) throw new Error("The website address is invalid.");
    let url: URL;
    try {
      url = new URL(operation.url);
    } catch {
      throw new Error("The website address is invalid.");
    }
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) throw new Error("OpenMuse can navigate only to ordinary public HTTP or HTTPS addresses.");
    const hostname = url.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    if (isPrivateHostname(hostname)) throw new Error("OpenMuse cannot navigate to a private or local network address.");
    return { kind: "navigate", url: url.href };
  }
  if (operation.kind === "keypress") {
    if (!ALLOWED_KEYS.has(operation.key)) throw new Error("That browser key is not available.");
    return operation;
  }
  if (operation.kind !== "click" && operation.kind !== "fill" && operation.kind !== "select") throw new Error("That browser operation is not available.");
  if (!operation.target || typeof operation.target !== "object" || !ALLOWED_ROLES.has(operation.target.role)
    || typeof operation.target.name !== "string" || !operation.target.name.trim() || operation.target.name.length > 160) {
    throw new Error("The browser target must have a supported role and short accessible name.");
  }
  const target = { ...operation.target, name: operation.target.name.trim() };
  if (operation.kind === "fill") {
    if (!["textbox", "searchbox", "spinbutton"].includes(target.role)) throw new Error("OpenMuse can fill only text, search, or number fields.");
    if (SENSITIVE_TARGET.test(target.name)) throw new Error("Use Take control to enter passwords, payment details, codes, or other secrets.");
    if (typeof operation.value !== "string" || operation.value.length > 2_000) throw new Error("The field value is too long.");
    return { ...operation, target };
  }
  if (operation.kind === "select") {
    if (target.role !== "combobox") throw new Error("OpenMuse can select options only in a combobox.");
    if (typeof operation.label !== "string" || !operation.label.trim() || operation.label.length > 160) throw new Error("The option label is invalid.");
    return { ...operation, target, label: operation.label.trim() };
  }
  return { ...operation, target };
}

export function operationReason(operation: BrowserOperation): string {
  switch (operation.kind) {
    case "observe": return "Read the current page";
    case "scroll": return `Scroll ${operation.direction}`;
    case "navigate": return `Open ${operation.url}`;
    case "click": return `Click ${operation.target.role} “${operation.target.name}”`;
    case "fill": return `Fill ${operation.target.role} “${operation.target.name}”`;
    case "select": return `Choose an option in ${operation.target.role} “${operation.target.name}”`;
    case "keypress": return `Press ${operation.key}`;
  }
}

export function operationProgram(operation: BrowserOperation, expectedPage?: string): string {
  const binding = expectedPage === undefined ? [] : [
    "const currentRawUrl = page.url();",
    "const currentParsedUrl = (() => { try { return new URL(currentRawUrl); } catch { return null; } })();",
    "const currentPage = currentParsedUrl && ['http:', 'https:'].includes(currentParsedUrl.protocol) ? `${currentParsedUrl.origin}${currentParsedUrl.pathname}${currentParsedUrl.search}${currentParsedUrl.hash}` : currentRawUrl;",
    `if (currentPage !== ${JSON.stringify(expectedPage)}) throw new Error('The page changed after approval.');`,
  ];
  switch (operation.kind) {
    case "observe":
      return [
        "const rawUrl = page.url();",
        "const parsedUrl = (() => { try { return new URL(rawUrl); } catch { return null; } })();",
        "const safeUrl = parsedUrl && ['http:', 'https:'].includes(parsedUrl.protocol) ? `${parsedUrl.origin}${parsedUrl.pathname}` : rawUrl;",
        "const sensitivePath = parsedUrl ? /(?:^|\\/)(?:account|auth|billing|checkout|login|orders?|payments?|profile|signin|wallet)(?:\\/|$)/i.test(parsedUrl.pathname) : true;",
        "const primary = page.locator('main, [role=main]');",
        "const hasPrimary = await primary.count().catch(() => 0) > 0;",
        "const rawText = sensitivePath || !hasPrimary ? '' : await primary.first().innerText({ timeout: 5_000 }).catch(() => '');",
        "const visibleText = rawText.replace(/\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b/gi, '[email redacted]').replace(/\\b(?:\\d[ -]*?){13,19}\\b/g, '[number redacted]').slice(0, 12000);",
        "const controls = sensitivePath ? [] : await page.locator('a, button, input, select, textarea, [role]').evaluateAll((nodes) => nodes.slice(0, 40).map((node) => { const tag = node.tagName.toLowerCase(); const type = (node.getAttribute('type') || '').toLowerCase(); const role = node.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'button' ? 'button' : tag === 'select' ? 'combobox' : tag === 'textarea' ? 'textbox' : type === 'checkbox' ? 'checkbox' : type === 'radio' ? 'radio' : type === 'search' ? 'searchbox' : type === 'number' ? 'spinbutton' : tag === 'input' ? 'textbox' : ''); const labels = Array.from(node.labels || []).map((label) => label.textContent || '').join(' '); const name = (node.getAttribute('aria-label') || labels || node.getAttribute('placeholder') || node.textContent || '').trim().slice(0, 160); return { role, name }; }).filter((item) => item.role && item.name)).catch(() => []);",
        "return { title: await page.title().catch(() => ''), url: safeUrl, visibleText, controls, ...(sensitivePath ? { textBlocked: true } : {}) };",
      ].join("\n");
    case "scroll":
      return `await page.mouse.wheel(0, ${operation.direction === "down" ? 600 : -600}); return { scrolled: ${JSON.stringify(operation.direction)} };`;
    case "navigate":
      return [...binding, `await page.goto(${JSON.stringify(operation.url)}); return { opened: ${JSON.stringify(operation.url)} };`].join("\n");
    case "click":
      return [...binding, targetProgram(operation.target), "await target.click(); return { clicked: true };"].join("\n");
    case "fill":
      return [
        ...binding,
        targetProgram(operation.target),
        "const fieldSafety = await target.evaluate((node) => ({ type: (node.getAttribute('type') || '').toLowerCase(), autocomplete: (node.getAttribute('autocomplete') || '').toLowerCase() }));",
        "if (fieldSafety.type === 'password' || /(?:^|\\s)(?:cc-|current-password|new-password|one-time-code)/.test(fieldSafety.autocomplete)) throw new Error('Use Take control to enter passwords, payment details, codes, or other secrets.');",
        `await target.fill(${JSON.stringify(operation.value)}); return { filled: true };`,
      ].join("\n");
    case "select":
      return [...binding, targetProgram(operation.target), `await target.selectOption({ label: ${JSON.stringify(operation.label)} }); return { selected: ${JSON.stringify(operation.label)} };`].join("\n");
    case "keypress":
      return [...binding, `await page.keyboard.press(${JSON.stringify(operation.key)}); return { pressed: ${JSON.stringify(operation.key)} };`].join("\n");
  }
}

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
    `const target = page.getByRole(${JSON.stringify(target.role)}, { name: ${JSON.stringify(target.name)}, exact: true });`,
    "if (await target.count() !== 1) throw new Error('The approved target is no longer unique.');",
  ].join("\n");
}
