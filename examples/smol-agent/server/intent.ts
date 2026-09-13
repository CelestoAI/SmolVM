import { randomUUID } from "node:crypto";
import { CATALOG } from "./catalog.js";
import type { IntentGrant } from "./types.js";

function priceBound(text: string): number | undefined {
  const match = text.match(/(?:under|below|less than|up to|at most)\s*(?:₹|rs\.?|inr)?\s*([\d,]+)/i);
  return match ? Number(match[1].replaceAll(",", "")) * 100 : undefined;
}

export function groundAddIntent(messageId: string, text: string): IntentGrant | undefined {
  if (!/\b(add|put)\b/i.test(text) || /\b(?:do not|don't|dont|never)\s+(?:add|put)\b/i.test(text)) return;
  const quantity = text.match(/\b(\d+)\s*(?:x|items?|pairs?|headphones?)\b/i)?.[1];
  if (quantity && Number(quantity) > 1) return;
  const normalized = text.toLowerCase();
  const product = CATALOG.find((entry) => normalized.includes(entry.name.toLowerCase()));
  const category = /wireless\s+headphones?|headphones?/i.test(text) ? "wireless-headphones" : undefined;
  if (!product && !category) return;
  const bound = priceBound(text) ?? product?.priceMinor;
  if (!bound) return;
  const variant = product && product.variants.length === 1
    ? { kind: "exact" as const, id: product.variants[0].id }
    : /\b(best|any|choose|pick)\b/i.test(text) ? { kind: "any" as const } : undefined;
  if (!variant) return;
  return {
    id: `grant-${randomUUID()}`, actionKind: "add_to_cart",
    subject: product ? { productId: product.id } : { categoryId: category! }, variant,
    maxQuantity: 1, maxUnitPriceMinor: bound, currency: "INR", sourceMessageId: messageId,
    expiresAt: new Date(Date.now() + 5 * 60_000).toISOString(), state: "available",
  };
}
