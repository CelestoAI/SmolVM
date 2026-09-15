import { randomUUID } from "node:crypto";
import type { Page } from "playwright-core";

export type TabOwner = "agent" | "paused" | "human" | "quarantined";

export interface BrowserTab {
  id: string;
  page: Page;
  owner: TabOwner;
  epoch: number;
  controlEpoch: string;
  openerTabId?: string;
}

export interface TabTarget {
  id: string;
  epoch: number;
  controlEpoch: string;
  pageIndex: number;
  pageBinding?: string;
  pageUrl?: string;
}

export function createTab(page: Page, owner: TabOwner, controlEpoch: string, openerTabId?: string): BrowserTab {
  return { id: `tab-${randomUUID()}`, page, owner, epoch: 1, controlEpoch, ...(openerTabId ? { openerTabId } : {}) };
}

export function bumpTab(tab: BrowserTab, owner = tab.owner, controlEpoch = tab.controlEpoch): BrowserTab {
  return { ...tab, owner, controlEpoch, epoch: tab.epoch + 1 };
}

export function publicTabUrl(page: Page): string {
  const raw = page.url();
  try {
    const parsed = new URL(raw);
    return ["http:", "https:"].includes(parsed.protocol) ? `${parsed.origin}${parsed.pathname}` : raw;
  } catch {
    return raw;
  }
}
