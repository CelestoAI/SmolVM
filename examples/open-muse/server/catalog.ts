import { createHash } from "node:crypto";

export const STOREFRONT_VERSION = "smol-shop-v1";
export const CATALOG = [
  { id: "soundarc-h7", categoryId: "wireless-headphones", name: "SoundArc H7", description: "Balanced over-ear headphones with 40-hour battery life.", priceMinor: 699_900, currency: "INR" as const, variants: [{ id: "black", name: "Black" }] },
  { id: "echopods-mini", categoryId: "wireless-headphones", name: "EchoPods Mini", description: "Compact earbuds with a pocket charging case.", priceMinor: 399_900, currency: "INR" as const, variants: [{ id: "white", name: "White" }] },
  { id: "aerobeat-pro", categoryId: "wireless-headphones", name: "AeroBeat Pro", description: "Premium noise cancelling headphones for travel.", priceMinor: 999_900, currency: "INR" as const, variants: [{ id: "navy", name: "Navy" }] },
] as const;

export type Product = (typeof CATALOG)[number];
export const CATALOG_CHECKSUM = createHash("sha256").update(JSON.stringify(CATALOG)).digest("hex");

export function productById(id: string): Product | undefined {
  return CATALOG.find((product) => product.id === id);
}

export function formatInr(minor: number): string {
  return new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 }).format(minor / 100);
}
