import type { BrowserContext, Page, Route } from "playwright-core";
import { CATALOG, CATALOG_CHECKSUM, STOREFRONT_VERSION, formatInr, productById } from "./catalog.js";
import type { CartLine } from "./types.js";

const ORIGIN = "http://shop.smol.test";

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]!);
}

function shell(title: string, content: string, cartCount: number): string {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escapeHtml(title)} · Smol Shop</title><style>
  *{box-sizing:border-box}body{margin:0;background:#f4f1ea;color:#1d1d1b;font-family:Inter,ui-sans-serif,system-ui,sans-serif}header{height:76px;background:#18211b;color:white;display:flex;align-items:center;justify-content:space-between;padding:0 48px}header a{color:white;text-decoration:none}.brand{font-family:Georgia,serif;font-size:27px}.cart{border:1px solid #607063;border-radius:24px;padding:10px 16px}main{max-width:1120px;margin:0 auto;padding:54px 44px}.eyebrow{font:700 11px ui-monospace,monospace;letter-spacing:.16em;text-transform:uppercase;color:#687068}h1{font:500 54px/1 Georgia,serif;margin:12px 0 18px}.lede{font-size:18px;line-height:1.6;color:#5c615c;max-width:660px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin-top:36px}.card{background:white;border:1px solid #d7d3c9;border-radius:3px;padding:24px;min-height:330px;display:flex;flex-direction:column}.art{height:126px;background:linear-gradient(145deg,#dfe9df,#adbcae);display:grid;place-items:center;font:48px Georgia,serif;color:#314135;margin-bottom:23px}.card h2{font:500 25px Georgia,serif;margin:0 0 8px}.card p{color:#666b66;line-height:1.45}.price{font-weight:750;font-size:20px;margin-top:auto}.actions{display:flex;gap:10px;margin-top:18px}button,.button{appearance:none;border:1px solid #1d1d1b;background:#1d1d1b;color:white;padding:12px 15px;font-weight:700;text-decoration:none;cursor:pointer}.secondary{background:white;color:#1d1d1b}.notice{border-left:3px solid #729378;background:white;padding:18px 22px;margin:24px 0}.line{display:flex;justify-content:space-between;border-bottom:1px solid #d7d3c9;padding:20px 0}.total{font-size:24px;font-weight:800}.review{background:#e9efe8;border:1px solid #b8c7b9;padding:28px;margin-top:28px}.muted{color:#6c716c}.empty{background:white;border:1px dashed #aaa59b;padding:42px;margin-top:30px;text-align:center}@media(max-width:800px){.grid{grid-template-columns:1fr}header{padding:0 20px}main{padding:32px 20px}h1{font-size:40px}}
  </style></head><body><header><a class="brand" href="/">Smol Shop</a><a class="cart" href="/cart">Cart · ${cartCount}</a></header><main>${content}</main></body></html>`;
}

function home(cart: CartLine[]): string {
  const cards = CATALOG.map((product) => `<article class="card" data-product-id="${product.id}"><div class="art">${product.name.slice(0, 1)}</div><h2>${escapeHtml(product.name)}</h2><p>${escapeHtml(product.description)}</p><div class="price">${formatInr(product.priceMinor)}</div><div class="actions"><a class="button secondary" href="/products/${product.id}">View details</a><button data-add="${product.id}">Add to cart</button></div></article>`).join("");
  return shell("Wireless audio", `<div class="eyebrow">Offline demo store · ${STOREFRONT_VERSION}</div><h1>Good sound,<br>small decisions.</h1><p class="lede">A deterministic storefront made for testing computer-using agents. Nothing here is charged, shipped, or connected to the public internet.</p><section class="grid">${cards}</section><script>document.addEventListener('click',async(e)=>{const id=e.target.dataset?.add;if(!id)return;e.preventDefault();const key=window.__smolActionKey;if(!key)return;await fetch('/api/cart',{method:'POST',headers:{'content-type':'application/json','x-smol-action-key':key},body:JSON.stringify({productId:id})});location.href='/cart'})</script>`, cart.length);
}

function detail(id: string, cart: CartLine[]): string {
  const product = productById(id);
  if (!product) return shell("Not found", `<h1>Product not found</h1><a href="/">Back to the catalog</a>`, cart.length);
  return shell(product.name, `<div class="eyebrow">Wireless headphones</div><h1>${escapeHtml(product.name)}</h1><p class="lede">${escapeHtml(product.description)}</p><div class="notice"><strong>${formatInr(product.priceMinor)}</strong><br><span class="muted">Color: ${escapeHtml(product.variants[0].name)} · Quantity: 1</span></div><button data-add="${product.id}">Add to cart</button> <a class="button secondary" href="/">Back</a><script>document.addEventListener('click',async(e)=>{const id=e.target.dataset?.add;if(!id)return;const key=window.__smolActionKey;if(!key)return;await fetch('/api/cart',{method:'POST',headers:{'content-type':'application/json','x-smol-action-key':key},body:JSON.stringify({productId:id})});location.href='/cart'})</script>`, cart.length);
}

function cartPage(cart: CartLine[], review = false): string {
  if (!cart.length) return shell("Cart", `<div class="eyebrow">Your cart</div><h1>Nothing here yet.</h1><div class="empty">Ask Smol Agent to find and add an item.</div>`, 0);
  const rows = cart.map((line) => { const product = productById(line.productId)!; return `<div class="line"><div><strong>${escapeHtml(product.name)}</strong><br><span class="muted">${escapeHtml(product.variants[0].name)} · Qty 1</span></div><strong>${formatInr(line.unitPriceMinor)}</strong></div>`; }).join("");
  const total = cart.reduce((sum, line) => sum + line.unitPriceMinor, 0);
  const reviewBox = review ? `<div class="review"><div class="eyebrow">Approved preview</div><h2>Order review only</h2><p>No payment or place-order endpoint exists in this demo.</p></div>` : `<button id="checkout">Review checkout</button><p class="muted">Smol Agent must ask before opening review.</p>`;
  return shell(review ? "Order review" : "Cart", `<div class="eyebrow">${review ? "Checkout" : "Your cart"}</div><h1>${review ? "Review your order." : "Cart"}</h1>${rows}<div class="line total"><span>Total</span><span>${formatInr(total)}</span></div>${reviewBox}`, cart.length);
}

export interface StorefrontController {
  page: Page;
  add(productId: string, idempotencyKey: string): Promise<{ receipt: string }>;
  navigate(route: string): Promise<void>;
  renderCurrent(): Promise<void>;
}

export async function installStorefront(context: BrowserContext, page: Page, state: { cart: CartLine[]; receipts: Map<string, string> }): Promise<StorefrontController> {
  let pendingKey: string | undefined;
  await context.route("**/*", async (route: Route) => {
    const url = new URL(route.request().url());
    if (url.origin !== ORIGIN) return route.abort("blockedbyclient");
    if (url.pathname === "/api/cart" && route.request().method() === "POST") {
      const key = route.request().headers()["x-smol-action-key"];
      if (!key || key !== pendingKey) return route.fulfill({ status: 403, body: "Action key rejected" });
      const body = route.request().postDataJSON() as { productId?: string };
      const product = body.productId ? productById(body.productId) : undefined;
      if (!product) return route.fulfill({ status: 400, body: "Unknown product" });
      const existing = state.receipts.get(key);
      if (existing) return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ receipt: existing }) });
      state.cart.splice(0, state.cart.length, { productId: product.id, variantId: product.variants[0].id, quantity: 1, unitPriceMinor: product.priceMinor });
      const receipt = `receipt-${key.slice(-12)}`;
      state.receipts.set(key, receipt);
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ receipt }) });
    }
    const path = url.pathname;
    const body = path === "/" ? home(state.cart) : path === "/cart" ? cartPage(state.cart) : path === "/review" ? cartPage(state.cart, true) : path.startsWith("/products/") ? detail(path.slice(10), state.cart) : shell("Not found", "<h1>Not found</h1>", state.cart.length);
    return route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", headers: { "x-storefront-version": STOREFRONT_VERSION, "x-catalog-checksum": CATALOG_CHECKSUM }, body });
  });
  await page.goto(`${ORIGIN}/`);
  return {
    page,
    async add(productId, idempotencyKey) {
      pendingKey = idempotencyKey;
      try {
        await page.evaluate((key) => { (window as typeof window & { __smolActionKey?: string }).__smolActionKey = key; }, idempotencyKey);
        const cartResponse = page.waitForResponse((response) => response.url() === `${ORIGIN}/api/cart` && response.request().method() === "POST");
        await page.locator(`[data-add="${productId}"]`).click();
        const response = await cartResponse;
        if (!response.ok()) throw new Error("The fixture rejected the cart update.");
        const receipt = state.receipts.get(idempotencyKey);
        if (!receipt) throw new Error("The fixture did not confirm the cart update.");
        return { receipt };
      } finally {
        pendingKey = undefined;
      }
    },
    async navigate(route) {
      const allowed = route === "/" || route === "/cart" || route === "/review" || /^\/products\/[a-z0-9-]+$/.test(route);
      if (!allowed) throw new Error("That route is outside the demo store.");
      await page.goto(`${ORIGIN}${route}`);
    },
    async renderCurrent() { await page.reload(); },
  };
}

export const STORE_ORIGIN = ORIGIN;
