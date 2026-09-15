export type BrowserTarget = {
  role: string;
  name: string;
  nth: number;
  locatorId: string;
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
