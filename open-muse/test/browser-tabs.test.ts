import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import type { Browser, BrowserContext, Frame, Page } from "playwright-core";
import type { ActionBroker } from "../server/broker.js";
import { ConversationManager } from "../server/manager.js";
import type { ConversationContext } from "../server/types.js";

class FakePage extends EventEmitter {
  closed = false;
  frame = {} as Frame;
  constructor(private address: string) { super(); }
  url() { return this.address; }
  isClosed() { return this.closed; }
  mainFrame() { return this.frame; }
  navigate(address: string) { this.address = address; this.emit("framenavigated", this.frame); }
  closePage() { this.closed = true; this.emit("close"); }
}

class FakeBrowserContext extends EventEmitter {
  constructor(readonly openPages: FakePage[]) { super(); }
  pages() { return this.openPages as unknown as Page[]; }
  async newPage() { const page = new FakePage("about:blank"); this.openPages.push(page); return page as unknown as Page; }
  popup(page: FakePage) { this.openPages.push(page); this.emit("page", page); }
}

function managerHarness() {
  const initial = new FakePage("https://example.com/start?secret=hidden");
  const browserContext = new FakeBrowserContext([initial]);
  const browser = {
    contexts: () => [browserContext as unknown as BrowserContext],
    isConnected: () => true,
    close: async () => undefined,
  } as unknown as Browser;
  const manager = new ConversationManager("", "gpt-5-mini", false, undefined, undefined, {
    connectOverCDP: async () => browser,
  });
  return { manager, initial, browserContext };
}

test("new popups stay quarantined until explicitly adopted", async () => {
  const { manager, browserContext } = managerHarness();
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    attachBrowser: (context: ConversationContext, url: string) => Promise<void>;
  };
  await internals.attachBrowser(internals.context, "http://browser.test");
  const popup = new FakePage("https://example.org/popup?token=hidden");
  browserContext.popup(popup);

  const quarantined = manager.snapshot(created.id).tabs.find((tab) => tab.owner === "quarantined")!;
  assert.equal(quarantined.url, "https://example.org/popup");
  assert.equal(quarantined.active, false);

  const adopted = await manager.adoptPopup(created.id, quarantined.id);
  assert.equal(adopted.tabs.find((tab) => tab.id === quarantined.id)?.owner, "agent");
  assert.equal(adopted.tabs.find((tab) => tab.id === quarantined.id)?.active, true);
});

test("private and local popups cannot be adopted", async () => {
  const { manager, browserContext } = managerHarness();
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    attachBrowser: (context: ConversationContext, url: string) => Promise<void>;
  };
  await internals.attachBrowser(internals.context, "http://browser.test");
  browserContext.popup(new FakePage("http://127.0.0.1/admin"));
  const popupId = manager.snapshot(created.id).tabs.find((tab) => tab.owner === "quarantined")!.id;

  await assert.rejects(manager.adoptPopup(created.id, popupId), (error: unknown) => (error as { status?: number }).status === 409);
  assert.equal(manager.snapshot(created.id).tabs.find((tab) => tab.id === popupId)?.owner, "quarantined");
});

test("tab navigation bumps its epoch and closed tabs leave the registry", async () => {
  const { manager, initial } = managerHarness();
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    attachBrowser: (context: ConversationContext, url: string) => Promise<void>;
  };
  await internals.attachBrowser(internals.context, "http://browser.test");
  const original = manager.snapshot(created.id).tabs[0]!;
  internals.context.observationId = "obs-before-navigation";
  internals.context.browserRefs.set("e1", {} as never);

  initial.navigate("https://example.com/next");
  assert.equal(manager.snapshot(created.id).tabs[0]?.epoch, original.epoch + 1);
  assert.equal(internals.context.observationId, "");
  assert.equal(internals.context.browserRefs.size, 0);
  initial.closePage();
  assert.deepEqual(manager.snapshot(created.id).tabs, []);
});

test("closing the active tab retains a paused replacement during takeover", async () => {
  const { manager, initial, browserContext } = managerHarness();
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    attachBrowser: (context: ConversationContext, url: string) => Promise<void>;
  };
  await internals.attachBrowser(internals.context, "http://browser.test");
  const popup = new FakePage("https://example.org/popup");
  browserContext.popup(popup);
  const popupId = manager.snapshot(created.id).tabs.find((tab) => tab.owner === "quarantined")!.id;
  await manager.adoptPopup(created.id, popupId);
  const activeId = internals.context.activeTabId!;
  for (const [tabId, tab] of internals.context.tabs) {
    internals.context.tabs.set(tabId, { ...tab, owner: "paused" });
  }
  internals.context.controlOwner = "pause_requested";

  (internals.context.tabs.get(activeId)!.page as unknown as FakePage).closePage();

  assert.notEqual(internals.context.activeTabId, activeId);
  assert.equal(internals.context.tabs.get(internals.context.activeTabId!)?.owner, "paused");
  assert.equal(manager.snapshot(created.id).tabs.filter((tab) => tab.active).length, 1);
});

test("closing every tab reconnects browser automation instead of using a missing page", async () => {
  const { manager, initial } = managerHarness();
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    attachBrowser: (context: ConversationContext, url: string) => Promise<void>;
    ensureBrowser: (context: ConversationContext) => Promise<void>;
  };
  await internals.attachBrowser(internals.context, "http://browser.test");
  initial.closePage();
  internals.context.sessionLifecycle = "ready";
  internals.context.computer = {
    display: { viewerUrl: "http://viewer.test" },
    browser: { cdpUrl: "http://browser.test", launch: async () => undefined },
  } as ConversationContext["computer"];

  await internals.ensureBrowser(internals.context);

  assert.equal(manager.snapshot(created.id).tabs.length, 1);
  assert.equal(manager.snapshot(created.id).tabs[0]?.active, true);
});

test("reattaching replaces a stale active tab instead of retaining its id", async () => {
  const { manager } = managerHarness();
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    attachBrowser: (context: ConversationContext, url: string) => Promise<void>;
  };
  await internals.attachBrowser(internals.context, "http://browser.test");
  const firstActive = internals.context.activeTabId!;
  internals.context.tabs.delete(firstActive);

  await internals.attachBrowser(internals.context, "http://browser.test");

  assert.notEqual(internals.context.activeTabId, firstActive);
  assert.ok(internals.context.tabs.has(internals.context.activeTabId!));
  assert.equal(manager.snapshot(created.id).tabs.filter((tab) => tab.active).length, 1);
});

test("takeover and return control update every owned tab epoch", async () => {
  const { manager, browserContext } = managerHarness();
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    attachBrowser: (context: ConversationContext, url: string) => Promise<void>;
    broker: (context: ConversationContext) => ActionBroker;
  };
  await internals.attachBrowser(internals.context, "http://browser.test");
  const popup = new FakePage("https://example.org/popup");
  browserContext.popup(popup);
  const popupId = manager.snapshot(created.id).tabs.find((tab) => tab.owner === "quarantined")!.id;
  await manager.adoptPopup(created.id, popupId);
  popup.navigate("https://example.org/profile.html");
  const before = new Map(manager.snapshot(created.id).tabs.map((tab) => [tab.id, tab.epoch]));
  internals.context.observationId = "obs-before-takeover";
  internals.context.browserRefs.set("e1", {} as never);

  const takeover = await manager.takeover(created.id);
  const humanTabs = manager.snapshot(created.id).tabs;
  assert.ok(humanTabs.every((tab) => tab.owner === "human" && tab.epoch > before.get(tab.id)!));
  assert.equal(internals.context.observationId, "");
  assert.equal(internals.context.browserRefs.size, 0);

  internals.context.observationId = "obs-before-resume";
  internals.context.browserRefs.set("e2", {} as never);
  const resumed = await manager.resume(created.id, takeover.controlEpoch);
  assert.ok(resumed.tabs.every((tab) => tab.owner === "agent" && tab.epoch > humanTabs.find((old) => old.id === tab.id)!.epoch));
  assert.equal(internals.context.observationId, "");
  assert.equal(internals.context.browserRefs.size, 0);

  internals.context.sessionLifecycle = "ready";
  internals.context.computer = {
    display: { viewerUrl: "http://viewer.test" },
    browser: { cdpUrl: "http://browser.test", launch: async () => undefined },
    exec: async () => ({
      ok: true,
      exitCode: 0,
      stderr: "",
      durationMs: 1,
      stdout: `SMOLVM_BROWSER_RESULT=${JSON.stringify({ ok: true, value: {
        programResult: {
          title: "Profile",
          url: "https://example.org/profile.html",
          pageBinding: "https://example.org/profile.html",
          snapshot: "",
          refs: [],
          truncated: false,
          textBlocked: true,
        },
        page: { title: "Profile", url: "https://example.org/profile.html" },
      } })}\n`,
    }),
  } as ConversationContext["computer"];
  const observation = await internals.broker(internals.context).runWebOperation({ kind: "observe" });
  assert.equal(observation.textBlocked, true);
  assert.equal(observation.snapshot, "");
  assert.deepEqual(observation.refs, []);
  assert.match(String(observation.observationId), /^obs-/);
});

test("stopping closes browser ownership and clears every tab", async () => {
  const { manager, browserContext } = managerHarness();
  const created = await manager.create();
  const internals = manager as unknown as {
    context: ConversationContext;
    attachBrowser: (context: ConversationContext, url: string) => Promise<void>;
  };
  await internals.attachBrowser(internals.context, "http://browser.test");
  browserContext.popup(new FakePage("https://example.org/popup"));
  assert.equal(manager.snapshot(created.id).tabs.length, 2);

  await manager.stop(created.id);

  assert.deepEqual(manager.snapshot(created.id).tabs, []);
});
