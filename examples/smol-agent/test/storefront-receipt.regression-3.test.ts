import assert from "node:assert/strict";
import test from "node:test";
import type { BrowserContext, Page, Route } from "playwright-core";
import { installStorefront } from "../server/storefront.js";

// Regression: ISSUE-004 — navigation disposed the response before receipt parsing
// Found by /qa on 2026-09-13
// Report: .gstack/qa-reports/qa-report-127-0-0-1-2026-09-13.md
test("cart completion uses the broker-owned receipt ledger", async () => {
  let handler: ((route: Route) => Promise<void>) | undefined;
  let actionKey = "";
  const fulfilled: Array<{ status?: number; body?: string }> = [];
  let resolveResponse!: (value: { url(): string; request(): { method(): string }; ok(): boolean }) => void;
  const response = new Promise<{ url(): string; request(): { method(): string }; ok(): boolean }>((resolve) => { resolveResponse = resolve; });
  const context = { route: async (_pattern: string, callback: (route: Route) => Promise<void>) => { handler = callback; } } as BrowserContext;
  const page = {
    goto: async () => undefined,
    evaluate: async (_callback: unknown, key: string) => { actionKey = key; },
    locator: () => ({ click: async () => {
      const route = {
        request: () => ({ url: () => "http://shop.smol.test/api/cart", method: () => "POST", headers: () => ({ "x-smol-action-key": actionKey }), postDataJSON: () => ({ productId: "soundarc-h7" }) }),
        fulfill: async (options: { status?: number; body?: string }) => {
          fulfilled.push(options);
          resolveResponse({ url: () => "http://shop.smol.test/api/cart", request: () => ({ method: () => "POST" }), ok: () => options.status === 200 });
        },
        abort: async () => undefined,
      } as unknown as Route;
      await handler!(route);
    } }),
    waitForResponse: async () => response,
    reload: async () => undefined,
  } as unknown as Page;
  const state = { cart: [], receipts: new Map<string, string>() };
  const store = await installStorefront(context, page, state);
  const result = await store.add("soundarc-h7", "cart-test-key");
  assert.equal(result.receipt, "receipt-art-test-key");
  assert.equal(state.cart.length, 1);
  assert.equal(fulfilled[0].status, 200);
});

test("a failed cart click clears its one-time action key", async () => {
  let handler: ((route: Route) => Promise<void>) | undefined;
  let actionKey = "";
  const fulfilled: Array<{ status?: number; body?: string }> = [];
  const context = {
    route: async (_pattern: string, callback: (route: Route) => Promise<void>) => { handler = callback; },
  } as BrowserContext;
  const page = {
    goto: async () => undefined,
    evaluate: async (_callback: unknown, key: string) => { actionKey = key; },
    waitForResponse: async () => new Promise(() => undefined),
    locator: () => ({ click: async () => { throw new Error("click failed"); } }),
  } as unknown as Page;
  const state = { cart: [], receipts: new Map<string, string>() };
  const store = await installStorefront(context, page, state);

  await assert.rejects(() => store.add("soundarc-h7", "cart-stale-key"), /click failed/);
  const staleRequest = {
    request: () => ({
      url: () => "http://shop.smol.test/api/cart",
      method: () => "POST",
      headers: () => ({ "x-smol-action-key": actionKey }),
      postDataJSON: () => ({ productId: "soundarc-h7" }),
    }),
    fulfill: async (options: { status?: number; body?: string }) => { fulfilled.push(options); },
    abort: async () => undefined,
  } as unknown as Route;
  await handler!(staleRequest);

  assert.equal(fulfilled[0].status, 403);
  assert.equal(state.cart.length, 0);
});
