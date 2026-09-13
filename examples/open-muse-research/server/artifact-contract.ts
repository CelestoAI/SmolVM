import { z } from "zod";
import type { ArtifactName, SourceRecord } from "./events.js";

export const ARTIFACT_NAMES: readonly ArtifactName[] = [
  "brief.md",
  "itinerary.md",
  "budget.csv",
  "sources.json",
];

export const sourceRecordSchema = z.object({
  id: z.string().regex(/^S\d{2}$/),
  url: z.string().url(),
  finalUrl: z.string().url(),
  title: z.string().min(1).max(300),
  retrievedAt: z.string().datetime(),
  contentSha256: z.string().regex(/^[a-f0-9]{64}$/),
});

export const budgetItemsSchema = z.array(z.object({
  category: z.string().min(1).max(80),
  item: z.string().min(1).max(160),
  quantity: z.number().int().positive(),
  unitCostInr: z.number().nonnegative().max(40_000),
  sourceId: z.string().regex(/^S\d{2}$/),
})).min(1).max(50);

export function validateArtifactName(name: string): ArtifactName {
  if (!(ARTIFACT_NAMES as readonly string[]).includes(name) || name.includes("/") || name.includes("\\")) {
    throw new Error("That artifact name is not allowed.");
  }
  return name as ArtifactName;
}

export function validateMarkdown(name: "brief.md" | "itinerary.md", content: string, sourceIds: Set<string>): void {
  if (Buffer.byteLength(content) > 1024 * 1024) throw new Error(`${name} is larger than 1 MiB.`);
  const markers = [...content.matchAll(/\[source:(S\d{2})\]/g)].map((match) => match[1]);
  for (const id of markers) if (!sourceIds.has(id)) throw new Error(`${name} refers to unknown source ${id}.`);
  if (/₹|\bINR\b|\bRs\.?\s*\d|\d[,.]?\d*\s*(?:rupees?|₹)/i.test(content) && markers.length === 0) {
    throw new Error(`${name} includes prices without source markers.`);
  }
  if (name === "itinerary.md" && ![1, 2, 3].every((day) => new RegExp(`^#{1,3}\\s+Day ${day}\\b`, "mi").test(content))) {
    throw new Error("itinerary.md must include Day 1, Day 2, and Day 3 headings.");
  }
}

export function serializeSources(sources: SourceRecord[]): string {
  return `${JSON.stringify([...sources].sort((a, b) => a.id.localeCompare(b.id)), null, 2)}\n`;
}
