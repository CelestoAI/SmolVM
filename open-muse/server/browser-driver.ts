import { randomUUID } from "node:crypto";
import type { Locator, Page } from "playwright-core";
import type { BrowserTarget, ExecutableBrowserOperation } from "./browser-operations.js";

export interface BrowserDriver {
  inspect(page: Page): Promise<{ binding: string; display: string }>;
  execute(page: Page, operation: ExecutableBrowserOperation, expectedPage?: string): Promise<unknown>;
}

export type BrowserFailureCode =
  | "CDP_DISCONNECTED"
  | "PAGE_CLOSED"
  | "OPERATION_TIMEOUT"
  | "ACCESSIBILITY_CAPTURE_FAILED"
  | "TEXT_EXTRACTION_FAILED"
  | "BROWSER_OPERATION_FAILED";

export class BrowserDriverError extends Error {
  constructor(readonly code: BrowserFailureCode, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "BrowserDriverError";
  }
}

const SENSITIVE_PATH = /(?:^|\/)(?:account|auth|billing|checkout|login|orders?|payments?|profile|signin|wallet)(?:[._-][^/]+)?(?:\/|$)/i;
const ACTION_ROLES = new Set(["button", "checkbox", "combobox", "link", "menuitem", "radio", "searchbox", "spinbutton", "textbox"]);

export async function inspectCurrentPage(page: Page): Promise<{ binding: string; display: string }> {
  assertPageReady(page);
  const rawUrl = page.url();
  const parsedUrl = parseUrl(rawUrl);
  return {
    binding: parsedUrl && isPublicPage(parsedUrl) ? `${parsedUrl.origin}${parsedUrl.pathname}${parsedUrl.search}${parsedUrl.hash}` : rawUrl,
    display: parsedUrl && isPublicPage(parsedUrl) ? `${parsedUrl.origin}${parsedUrl.pathname}` : rawUrl,
  };
}

export async function executeBrowserOperation(
  page: Page,
  operation: ExecutableBrowserOperation,
  expectedPage?: string,
): Promise<unknown> {
  try {
    assertPageReady(page);
    if (expectedPage !== undefined && (await inspectCurrentPage(page)).binding !== expectedPage) {
      throw new Error("The approved page changed before execution.");
    }

    switch (operation.kind) {
      case "observe": return observe(page);
      case "extract": return extract(page, operation.target);
      case "scroll":
        await page.mouse.wheel(0, operation.direction === "down" ? 600 : -600);
        return { scrolled: operation.direction, observation: await observe(page) };
      case "navigate":
        await page.goto(operation.url);
        return { opened: operation.url, observation: await observe(page) };
      case "click": {
        const target = await resolveTarget(page, operation.target);
        await target.click();
        return { clicked: true };
      }
      case "fill": {
        const target = await resolveTarget(page, operation.target);
        const fieldSafety = await target.evaluate((node) => ({
          type: (node.getAttribute("type") || "").toLowerCase(),
          autocomplete: (node.getAttribute("autocomplete") || "").toLowerCase(),
        }));
        if (fieldSafety.type === "password" || /(?:^|\s)(?:cc-|current-password|new-password|one-time-code)/.test(fieldSafety.autocomplete)) {
          throw new Error("Use Take control to enter passwords, payment details, codes, or other secrets.");
        }
        await target.fill(operation.value);
        return { filled: true };
      }
      case "select": {
        const target = await resolveTarget(page, operation.target);
        await target.selectOption({ label: operation.label });
        return { selected: operation.label };
      }
      case "keypress":
        await page.keyboard.press(operation.key);
        return { pressed: operation.key };
    }
  } catch (error) {
    if (error instanceof BrowserDriverError || isPolicyError(error)) throw error;
    throw classifyBrowserError(page, error);
  }
}

export const hostBrowserDriver: BrowserDriver = {
  inspect: inspectCurrentPage,
  execute: executeBrowserOperation,
};

async function observe(page: Page): Promise<Record<string, unknown>> {
  const { binding, display } = await inspectCurrentPage(page);
  const parsedUrl = parseUrl(page.url());
  const sensitive = parsedUrl ? SENSITIVE_PATH.test(parsedUrl.pathname) : true;
  let rawSnapshot = "";
  if (!sensitive) {
    try {
      rawSnapshot = await page.locator("body").ariaSnapshot();
    } catch (error) {
      const failure = classifyBrowserError(page, error);
      if (failure.code !== "BROWSER_OPERATION_FAILED") throw failure;
      throw new BrowserDriverError(
        "ACCESSIBILITY_CAPTURE_FAILED",
        "Chromium could not produce an accessibility snapshot; try again or use Take control.",
        { cause: error },
      );
    }
  }

  const refs: Array<Record<string, unknown>> = [];
  const counts = new Map<string, number>();
  const lines: string[] = [];
  let length = 0;
  let truncated = false;
  for (const line of rawSnapshot.split("\n")) {
    const match = line.match(/^(\s*-\s+)([a-z][a-z0-9]*)\s+"((?:[^"\\]|\\.)*)"(.*)$/);
    let rendered = redact(line);
    let candidate: Record<string, unknown> | undefined;
    if (match && refs.length < 100) {
      let name: string;
      try { name = JSON.parse(`"${match[3]}"`) as string; } catch { name = match[3]; }
      const role = match[2];
      const key = `${role}\u0000${name}`;
      const nth = counts.get(key) ?? 0;
      counts.set(key, nth + 1);
      const locatorId = randomUUID();
      const marked = await page.getByRole(role as Parameters<Page["getByRole"]>[0], { name, exact: true }).nth(nth)
        .evaluate((node, id) => node.setAttribute("data-smolvm-browser-ref", id), locatorId)
        .then(() => true, () => false);
      if (marked) {
        const publicName = redact(name).slice(0, 160);
        candidate = { ref: `e${refs.length + 1}`, role, name, publicName, nth, locatorId, actionable: ACTION_ROLES.has(role) };
        rendered = `${match[1]}${role} "${publicName}" [ref=${candidate.ref}]${match[4]}`;
      }
    }
    const addition = `${rendered}\n`;
    if (length + addition.length > 12_000) {
      truncated = true;
      break;
    }
    lines.push(rendered);
    length += addition.length;
    if (candidate) refs.push(candidate);
  }
  return { title: await safeTitle(page), url: display, pageBinding: binding, snapshot: lines.join("\n"), refs, truncated, ...(sensitive ? { textBlocked: true } : {}) };
}

async function extract(page: Page, target?: BrowserTarget): Promise<Record<string, unknown>> {
  const { binding, display } = await inspectCurrentPage(page);
  const parsedUrl = parseUrl(page.url());
  const sensitive = parsedUrl ? SENSITIVE_PATH.test(parsedUrl.pathname) : true;
  let rawText = "";
  if (!sensitive) {
    try {
      rawText = await (target ? await resolveTarget(page, target) : page.locator("body")).innerText({ timeout: 5_000 });
    } catch (error) {
      const failure = classifyBrowserError(page, error);
      if (failure.code !== "BROWSER_OPERATION_FAILED") throw failure;
      throw new BrowserDriverError(
        "TEXT_EXTRACTION_FAILED",
        "Chromium could not extract text from the page; try again or use Take control.",
        { cause: error },
      );
    }
  }
  return {
    title: await safeTitle(page), url: display, pageBinding: binding,
    text: redact(rawText).slice(0, 16_000), truncated: rawText.length > 16_000,
    ...(sensitive ? { textBlocked: true } : {}),
  };
}

async function resolveTarget(page: Page, target: BrowserTarget): Promise<Locator> {
  const candidates = page.locator(`[data-smolvm-browser-ref="${target.locatorId}"]`)
    .and(page.getByRole(target.role as Parameters<Page["getByRole"]>[0], { name: target.name, exact: true }));
  if (await candidates.count() !== 1) throw new Error("The observed target is no longer available.");
  return candidates.first();
}

function assertPageReady(page: Page): void {
  if (page.isClosed()) throw new BrowserDriverError("PAGE_CLOSED", "The controlled browser tab is closed; open or adopt a tab, then try again.");
  const browser = page.context().browser();
  if (browser && !browser.isConnected()) {
    throw new BrowserDriverError("CDP_DISCONNECTED", "The browser automation connection was lost; restart the disposable browser, then try again.");
  }
}

function classifyBrowserError(page: Page, error: unknown): BrowserDriverError {
  const message = error instanceof Error ? error.message : String(error);
  if (page.isClosed() || /(?:page|target|context|browser).*(?:closed|crashed)/i.test(message)) {
    return new BrowserDriverError("PAGE_CLOSED", "The controlled browser tab is closed; open or adopt a tab, then try again.", { cause: error });
  }
  if (/disconnected|connection.*closed|ECONNRESET|ECONNREFUSED/i.test(message)) {
    return new BrowserDriverError("CDP_DISCONNECTED", "The browser automation connection was lost; restart the disposable browser, then try again.", { cause: error });
  }
  if (/timeout|timed out/i.test(message)) {
    return new BrowserDriverError("OPERATION_TIMEOUT", "The browser operation timed out; check the visible page, then try again.", { cause: error });
  }
  return new BrowserDriverError("BROWSER_OPERATION_FAILED", "Chromium could not complete the browser operation; check the visible page, then try again.", { cause: error });
}

function isPolicyError(error: unknown): boolean {
  return error instanceof Error && /^(?:The approved page changed|The observed target is no longer available|Use Take control)/.test(error.message);
}

function parseUrl(value: string): URL | undefined {
  try { return new URL(value); } catch { return undefined; }
}

function isPublicPage(url: URL): boolean {
  return url.protocol === "http:" || url.protocol === "https:";
}

function redact(value: string): string {
  return value
    .replace(/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi, "[email redacted]")
    .replace(/\b(?:\d[ -]*?){13,19}\b/g, "[number redacted]");
}

async function safeTitle(page: Page): Promise<string> {
  return page.title().catch(() => "");
}
