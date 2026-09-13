import assert from "node:assert/strict";
import test from "node:test";
import type { SandboxClient, SandboxStatus } from "@celestoai/smolvm";
import { initializeSandbox, researchTools, type ToolState } from "../server/tools.js";

class ToolSandbox implements SandboxClient {
  readonly id = "sbx-tools";
  readonly status: SandboxStatus = "running";
  readonly calls: Array<readonly string[]> = [];
  readonly values = new Map<string, string>([["/workspace/open-muse-research/research/findings.jsonl", ""]]);
  readonly files = {
    read: async (path: string) => this.values.get(path) ?? "",
    write: async (path: string, value: string | Uint8Array) => { this.values.set(path, typeof value === "string" ? value : Buffer.from(value).toString()); },
    upload: async () => {},
    download: async () => {},
  };
  async exec(command: string | readonly string[]) {
    assert.ok(Array.isArray(command), "tool commands must use argv arrays");
    const argv = command as readonly string[];
    this.calls.push(argv);
    const output = argv.at(-1)!;
    this.values.set(output, JSON.stringify({
      url: argv.at(-2), finalUrl: argv.at(-2), title: "Example", retrievedAt: "2026-09-12T12:00:00.000Z",
      contentSha256: "a".repeat(64), text: "Public evidence only.",
    }));
    return { ok: true, exitCode: 0, stdout: "", stderr: "", durationMs: 1 };
  }
  async delete() {}
}

test("sandbox setup uploads guest scripts sequentially", async () => {
  let activeWrites = 0;
  let maxActiveWrites = 0;
  const sandbox = {
    id: "sbx-setup",
    status: "running",
    files: {
      read: async () => "",
      write: async () => {
        activeWrites += 1;
        maxActiveWrites = Math.max(maxActiveWrites, activeWrites);
        await new Promise<void>((resolve) => setImmediate(resolve));
        activeWrites -= 1;
      },
      upload: async () => {},
      download: async () => {},
    },
    exec: async () => ({ ok: true, exitCode: 0, stdout: "", stderr: "", durationMs: 1 }),
    delete: async () => {},
  } as unknown as SandboxClient;
  const state: ToolState = {
    sandbox,
    signal: new AbortController().signal,
    sources: [], findings: [], written: new Set(),
    emit: () => {}, phase: () => {},
  };
  await initializeSandbox(state, { "one.py": "one", "two.py": "two", "three.py": "three" });
  assert.equal(maxActiveWrites, 1);
});

test("fetch tool bounds guest execution, passes the URL as one argv value, and redacts query strings", async () => {
  const sandbox = new ToolSandbox();
  const events: Array<{ type: string; source?: { url: string } }> = [];
  const state: ToolState = {
    sandbox,
    signal: new AbortController().signal,
    sources: [], findings: [], written: new Set(),
    emit: (event) => events.push(event as { type: string; source?: { url: string } }),
    phase: () => {},
  };
  const fetchTool = researchTools(state).find((tool) => tool.name === "fetch_public_page")!;
  const url = "https://example.com/travel?q=private";
  const output = await fetchTool.execute("call-1", { url, purpose: "Check a fare" });
  assert.deepEqual(sandbox.calls[0].slice(0, 5), ["timeout", "--signal=TERM", "--kill-after=2s", "20s", "python3"]);
  assert.equal(sandbox.calls[0].at(-2), url);
  assert.equal(state.sources[0].url, url);
  assert.equal(events.find((event) => event.type === "source.saved")?.source?.url, "https://example.com");
  assert.match(output.content[0].type === "text" ? output.content[0].text : "", /untrustedSourceMaterial/);
});
