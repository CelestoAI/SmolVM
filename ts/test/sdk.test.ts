import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { SmolVM, SmolVMError } from "../src/index.js";
import { ProcessTransport } from "../src/transport.js";
import type { SmolVMTransport } from "../src/index.js";

class FakeTransport implements SmolVMTransport {
  readonly calls: Array<{ path: string; init?: RequestInit }> = [];
  closeCount = 0;
  files = new Map<string, Uint8Array>();

  async request<T>(path: string, init?: RequestInit): Promise<T> {
    this.calls.push({ path, init });
    if (path === "/sdk/v1/capabilities") return { protocol_version: 1, capabilities: ["sandbox.create", "sandbox.delete", "sandbox.exec", "files.read", "files.write", "browser.create", "browser.delete", "browser.endpoints", "browser.exec", "browser.files", "browser.events", "computer.create", "computer.delete", "computer.endpoints", "computer.exec", "computer.files", "computer.browser", "computer.events", "events"] } as T;
    if (path === "/sdk/v1/diagnostics") return { protocol_version: 1, runtime_version: "test", python_version: "3.13", platform: "darwin-arm64", supported: true, problems: [] } as T;
    if (path === "/sandboxes") return { id: "sbx-test", status: "running" } as T;
    if (path === "/browser-sessions") return {
      session_id: "browser-test",
      sandbox_id: "vm-browser-test",
      status: "ready",
      cdp_url: "http://127.0.0.1:9222",
      viewer_url: "http://127.0.0.1:6080/vnc.html",
      display_url: "vnc://127.0.0.1:5900",
      profile_id: null,
    } as T;
    if (path === "/computers") return {
      computer_id: "computer-test",
      sandbox_id: "vm-computer-test",
      template: "linux-desktop",
      status: "ready",
      capabilities: ["display.viewer", "display.vnc", "browser.cdp", "sandbox.exec", "sandbox.files"],
      display: {
        viewer_url: "http://127.0.0.1:6081/vnc.html",
        vnc_url: "vnc://127.0.0.1:5901",
      },
      browser: { status: "ready", cdp_url: "http://127.0.0.1:9223" },
    } as T;
    if (path.endsWith("/browser/launch")) {
      return { status: "ready", cdp_url: "http://127.0.0.1:9223" } as T;
    }
    if (path.endsWith("/exec")) return { exit_code: 7, stdout: "out", stderr: "err", duration_ms: 12 } as T;
    if (path.includes("/files") && init?.method === "PUT") {
      const body = init.body;
      if (body instanceof ArrayBuffer) this.files.set(path, new Uint8Array(body));
      else if (body instanceof Uint8Array) this.files.set(path, body);
    }
    return undefined as T;
  }

  async requestBytes(path: string): Promise<Uint8Array> {
    this.calls.push({ path });
    return this.files.get(path) ?? new Uint8Array();
  }

  async requestStream(path: string, content: AsyncIterable<Uint8Array>, size: number): Promise<void> {
    this.calls.push({ path, init: { method: "PUT", headers: { "content-length": String(size) } } });
    const chunks: Uint8Array[] = [];
    for await (const chunk of content) chunks.push(chunk);
    const bytes = new Uint8Array(chunks.reduce((total, chunk) => total + chunk.byteLength, 0));
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    this.files.set(path, bytes);
  }

  async close(): Promise<void> { this.closeCount += 1; }
}

test("creates Ubuntu by default and maps command results", async () => {
  const transport = new FakeTransport();
  const events: string[] = [];
  const client = new SmolVM({ transport, onEvent: (event) => events.push(event.type) });
  const sandbox = await client.sandboxes.create({ network: { mode: "off" } });
  const result = await sandbox.exec(["printf", "%s", "a b"]);

  const createBody = JSON.parse(String(transport.calls.find((call) => call.path === "/sandboxes")?.init?.body));
  const execBody = JSON.parse(String(transport.calls.find((call) => call.path.endsWith("/exec"))?.init?.body));
  assert.equal(createBody.os, "ubuntu");
  assert.deepEqual(createBody.network, { mode: "off" });
  assert.equal(execBody.shell, "raw");
  assert.equal(execBody.command, "'printf' '%s' 'a b'");
  assert.deepEqual(result, { ok: false, exitCode: 7, stdout: "out", stderr: "err", durationMs: 12 });
  assert.deepEqual(events, ["sandbox.starting", "sandbox.ready", "command.started", "command.completed"]);
});

test("creates a ready live browser session and deletes it once", async () => {
  const transport = new FakeTransport();
  const events: string[] = [];
  const client = new SmolVM({ transport, onEvent: (event) => events.push(event.type) });
  const browser = await client.browsers.create({
    sessionId: "browser-test",
    mode: "live",
    backend: "qemu",
    viewport: { width: 1440, height: 900 },
    allowDownloads: false,
    network: { mode: "off" },
  });

  const body = JSON.parse(String(transport.calls.find((call) => call.path === "/browser-sessions")?.init?.body));
  assert.deepEqual(body.viewport, { width: 1440, height: 900 });
  assert.deepEqual(body.network, { mode: "off" });
  assert.equal(body.allow_downloads, false);
  assert.equal(browser.status, "ready");
  assert.equal(browser.cdpUrl, "http://127.0.0.1:9222");
  assert.equal(browser.viewerUrl, "http://127.0.0.1:6080/vnc.html");
  assert.equal(browser.displayUrl, "vnc://127.0.0.1:5900");

  await browser.files.write("/workspace/input.txt", "hello");
  assert.equal(await browser.files.read("/workspace/input.txt"), "hello");
  assert.ok(transport.calls.some((call) => call.path.startsWith("/browser-sessions/browser-test/files?")));

  const execResult = await browser.exec(["printf", "%s", "hello world"]);
  const execBody = JSON.parse(String(transport.calls.find((call) => call.path === "/browser-sessions/browser-test/exec")?.init?.body));
  assert.equal(execBody.shell, "raw");
  assert.equal(execBody.command, "'printf' '%s' 'hello world'");
  assert.deepEqual(execResult, { ok: false, exitCode: 7, stdout: "out", stderr: "err", durationMs: 12 });

  const deletion = browser.delete();
  assert.equal(browser.status, "stopping");
  await assert.rejects(
    () => browser.exec("echo too-late"),
    /call smolvm\.browsers\.create\(\) to create a replacement/,
  );
  await assert.rejects(() => browser.files.read("/workspace/input.txt"), /not ready/);
  await Promise.all([deletion, browser.delete()]);
  assert.equal(browser.status, "deleted");
  assert.equal(transport.calls.filter((call) => call.path === "/browser-sessions/browser-test" && call.init?.method === "DELETE").length, 1);
  assert.deepEqual(events, ["browser.starting", "browser.ready", "command.started", "command.completed", "browser.stopping", "browser.deleted"]);
});

test("creates a Linux computer with grouped display and browser access", async () => {
  const transport = new FakeTransport();
  const events: string[] = [];
  const client = new SmolVM({ transport, onEvent: (event) => events.push(event.type) });
  const computer = await client.computers.create({
    name: "computer-test",
    display: { width: 1440, height: 900 },
    resources: { memoryMiB: 4096, diskMiB: 12288, vcpus: 2 },
    network: { mode: "off" },
    workspace: [{ hostPath: "/tmp/project", guestPath: "/workspace", writable: false }],
  });

  const body = JSON.parse(String(
    transport.calls.find((call) => call.path === "/computers")?.init?.body,
  ));
  assert.equal(body.template, "linux-desktop");
  assert.deepEqual(body.resources, { memory_mib: 4096, disk_mib: 12288, vcpus: 2 });
  assert.deepEqual(body.workspace, [{
    host_path: "/tmp/project",
    guest_path: "/workspace",
    writable: false,
  }]);
  assert.equal(computer.display.viewerUrl, "http://127.0.0.1:6081/vnc.html");
  assert.equal(computer.display.vncUrl, "vnc://127.0.0.1:5901");
  assert.equal(computer.browser.cdpUrl, "http://127.0.0.1:9223");
  await computer.browser.launch();
  assert.ok(transport.calls.some((call) => call.path.endsWith("/browser/launch")));

  const result = await computer.exec(["printf", "%s", "desktop"]);
  assert.equal(result.exitCode, 7);
  await computer.delete();
  assert.equal(computer.status, "deleted");
  assert.deepEqual(events, [
    "computer.starting",
    "computer.ready",
    "command.started",
    "command.completed",
    "computer.stopping",
    "computer.deleted",
  ]);
});

test("computer cleanup stays retryable when the first delete fails", async () => {
  class RetryDeleteTransport extends FakeTransport {
    attempts = 0;
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path === "/computers/computer-test" && init?.method === "DELETE") {
        this.calls.push({ path, init });
        if (this.attempts++ === 0) {
          throw new SmolVMError("cleanup_failed", "busy", { operation: "computer.delete" });
        }
        return undefined as T;
      }
      return super.request(path, init);
    }
  }
  const transport = new RetryDeleteTransport();
  const client = new SmolVM({ transport });
  const computer = await client.computers.create({ name: "computer-test" });

  await assert.rejects(() => client.close(), /not fully deleted/);
  assert.equal(computer.status, "error");
  assert.equal(transport.closeCount, 0);

  await client.close();
  assert.equal(computer.status, "deleted");
  assert.equal(transport.closeCount, 1);
  assert.equal(transport.attempts, 2);
});

test("computer creation rejects a response without a usable display", async () => {
  class MissingDisplayTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path === "/computers") {
        return {
          computer_id: "computer-test",
          sandbox_id: "vm-computer-test",
          template: "linux-desktop",
          status: "ready",
          capabilities: [],
          display: { viewer_url: "", vnc_url: "" },
          browser: { status: "closed", cdp_url: null },
        } as T;
      }
      return super.request(path, init);
    }
  }
  const client = new SmolVM({ transport: new MissingDisplayTransport() });

  await assert.rejects(
    () => client.computers.create(),
    (error: unknown) => error instanceof SmolVMError
      && error.code === "computer_endpoint_unavailable",
  );
});

test("deleted computers reject commands, files, and browser relaunch", async () => {
  const client = new SmolVM({ transport: new FakeTransport() });
  const computer = await client.computers.create({ name: "computer-test" });
  await computer.delete();

  await assert.rejects(() => computer.exec("echo late"), /not ready/);
  await assert.rejects(() => computer.files.read("/workspace/late.txt"), /not ready/);
  await assert.rejects(() => computer.browser.launch(), /not ready/);
  await client.close();
});

test("computer files round trip text through the grouped file API", async () => {
  const transport = new FakeTransport();
  const client = new SmolVM({ transport });
  const computer = await client.computers.create({ name: "computer-test" });

  await computer.files.write("/workspace/note.txt", "desktop");
  assert.equal(await computer.files.read("/workspace/note.txt"), "desktop");
  assert.ok(transport.calls.some((call) =>
    call.path.startsWith("/computers/computer-test/files?")
  ));
  await client.close();
});

test("computer commands reject empty argv and invalid timeouts before transport", async () => {
  const transport = new FakeTransport();
  const client = new SmolVM({ transport });
  const computer = await client.computers.create({ name: "computer-test" });
  const callsBefore = transport.calls.length;

  await assert.rejects(() => computer.exec([]), /at least one item/);
  await assert.rejects(() => computer.exec("echo late", { timeoutMs: 0 }), /1 to 3,600,000/);
  assert.equal(transport.calls.length, callsBefore);
  await client.close();
});

test("computer timeout records server-confirmed deletion", async () => {
  class ComputerTimeoutTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path === "/computers/computer-test/exec") {
        throw new SmolVMError("command_timeout", "timed out and deleted", {
          operation: "computer.exec",
          actual: { sandboxDeleted: true },
        });
      }
      return super.request(path, init);
    }
  }
  const client = new SmolVM({ transport: new ComputerTimeoutTransport() });
  const computer = await client.computers.create({ name: "computer-test" });

  await assert.rejects(
    () => computer.exec("sleep 60"),
    (error: unknown) => error instanceof SmolVMError
      && error.code === "command_timeout"
      && error.actual?.computerDeleted === true,
  );
  assert.equal(computer.status, "deleted");
});

test("an already-aborted computer command does not start or delete the computer", async () => {
  const transport = new FakeTransport();
  const events: string[] = [];
  const client = new SmolVM({ transport, onEvent: (event) => events.push(event.type) });
  const computer = await client.computers.create({ name: "computer-test" });
  const controller = new AbortController();
  controller.abort();

  await assert.rejects(
    () => computer.exec("sleep 60", { signal: controller.signal }),
    (error: unknown) => error instanceof SmolVMError && error.code === "command_aborted",
  );
  assert.equal(computer.status, "ready");
  assert.equal(transport.calls.some((call) => call.path.endsWith("/exec")), false);
  assert.equal(transport.calls.some((call) => call.path.endsWith("/cancel")), false);
  assert.deepEqual(events, ["computer.starting", "computer.ready"]);
});

test("browser computers upload and atomically download files", async () => {
  const directory = await mkdtemp(join(tmpdir(), "smolvm-browser-files-"));
  const localInput = join(directory, "input.bin");
  const localOutput = join(directory, "nested", "output.bin");
  const content = new Uint8Array([0, 1, 2, 255]);
  const client = new SmolVM({ transport: new FakeTransport() });
  const computer = await client.browsers.create();

  try {
    await writeFile(localInput, content);
    await computer.files.upload(localInput, "/workspace/input.bin");
    await computer.files.download("/workspace/input.bin", localOutput);

    assert.deepEqual(new Uint8Array(await readFile(localOutput)), content);
  } finally {
    await client.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test("browser timeout records the server-confirmed session deletion", async () => {
  class BrowserTimeoutTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path.includes("/browser-sessions/") && path.endsWith("/exec")) {
        throw new SmolVMError("command_timeout", "timed out and deleted", {
          operation: "POST browser exec",
          actual: { sandboxDeleted: true },
        });
      }
      return super.request(path, init);
    }
  }
  const events: string[] = [];
  const client = new SmolVM({
    transport: new BrowserTimeoutTransport(),
    onEvent: (event) => events.push(event.type),
  });
  const browser = await client.browsers.create();

  await assert.rejects(
    () => browser.exec("sleep 60"),
    (error: unknown) => error instanceof SmolVMError
      && error.code === "command_timeout"
      && error.actual?.sandboxDeleted === true,
  );

  assert.equal(browser.status, "deleted");
  assert.equal(events.at(-1), "browser.deleted");
  await assert.rejects(
    () => browser.exec("echo retry"),
    (error: unknown) => error instanceof SmolVMError && error.code === "browser_deleted",
  );
});

test("browser sessions require browser runtime capabilities without breaking sandboxes", async () => {
  class SandboxOnlyTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path === "/sdk/v1/capabilities") return {
        protocol_version: 1,
        capabilities: ["sandbox.create", "sandbox.delete", "sandbox.exec", "files.read", "files.write", "events"],
      } as T;
      return super.request(path, init);
    }
  }
  const client = new SmolVM({ transport: new SandboxOnlyTransport() });
  assert.equal((await client.sandboxes.create()).status, "running");
  await assert.rejects(
    () => client.browsers.create(),
    (error: unknown) => error instanceof SmolVMError
      && error.code === "protocol_incompatible"
      && String(error.actual?.missingCapabilities).includes("browser.create"),
  );
});

test("validates bridge request deadlines", () => {
  assert.throws(
    () => new SmolVM({ transport: new FakeTransport(), requestTimeoutMs: 0 }),
    /requestTimeoutMs must be an integer/,
  );
  assert.throws(
    () => new SmolVM({ transport: new FakeTransport(), createTimeoutMs: 0 }),
    /createTimeoutMs must be an integer/,
  );
});

test("process transport preserves computer error codes from the runtime", async () => {
  const server = createServer((_request, response) => {
    response.statusCode = 409;
    response.setHeader("content-type", "application/json");
    response.setHeader("x-smolvm-error-code", "computer_already_exists");
    response.end(JSON.stringify({ detail: "Computer 'computer-demo' already exists." }));
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert(address && typeof address === "object");
  const transport = new ProcessTransport("unused", 1_000, 1_000, 1_000, false, () => {});
  Object.assign(transport, {
    startPromise: Promise.resolve(),
    baseUrl: `http://127.0.0.1:${address.port}`,
    token: "test-token",
  });

  try {
    await assert.rejects(
      () => transport.request("/computers", { method: "POST" }),
      (error: unknown) => error instanceof SmolVMError
        && error.code === "computer_already_exists",
    );
  } finally {
    await transport.close();
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test("sandbox creation timeout invalidates every handle in its SDK session", async () => {
  let creations = 0;
  const server = createServer((request, response) => {
    if (request.url === "/sdk/v1/capabilities") {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({
        protocol_version: 1,
        capabilities: ["sandbox.create", "sandbox.delete", "sandbox.exec", "files.read", "files.write", "events"],
      }));
      return;
    }
    if (request.url === "/sandboxes" && ++creations <= 2) {
      response.setHeader("content-type", "application/json");
      response.end(JSON.stringify({ id: `sbx-${creations}`, status: "running" }));
    }
    // Keep the third creation request open until its deadline.
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert(address && typeof address === "object");
  const transport = new ProcessTransport("unused", 1_000, 20, 1_000, false, () => {});
  Object.assign(transport, {
    startPromise: Promise.resolve(),
    baseUrl: `http://127.0.0.1:${address.port}`,
    token: "test-token",
  });
  const deleted: string[] = [];
  const client = new SmolVM({
    transport,
    onEvent: (event) => {
      if (event.type === "sandbox.deleted") deleted.push(event.sandboxId);
    },
  });
  const first = await client.sandboxes.create();
  const second = await client.sandboxes.create();

  try {
    await assert.rejects(
      () => client.sandboxes.create(),
      (error: unknown) => error instanceof SmolVMError
        && error.code === "sandbox_create_failed"
        && error.actual?.createTimeoutMs === 20
        && error.actual?.sessionClosed === true,
    );
    assert.equal(first.status, "deleted");
    assert.equal(second.status, "deleted");
    assert.deepEqual(deleted, [first.id, second.id]);
    await client.close();
    assert.deepEqual(deleted, [first.id, second.id]);
  } finally {
    server.closeAllConnections();
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
});

test("file helpers use content endpoints and validate paths", async () => {
  const client = new SmolVM({ transport: new FakeTransport() });
  const sandbox = await client.sandboxes.create();
  await sandbox.files.write("/workspace/input.txt", "hello");
  assert.equal(await sandbox.files.read("/workspace/input.txt"), "hello");
  await assert.rejects(
    () => sandbox.files.read("relative.txt"),
    (error: unknown) => error instanceof SmolVMError
      && error.code === "invalid_path"
      && error.message.includes(`Sandbox '${sandbox.id}'`)
      && error.message.includes("files.read('/workspace/file')"),
  );
});

test("a custom image does not receive an implicit OS override", async () => {
  const transport = new FakeTransport();
  const client = new SmolVM({ transport });
  await client.sandboxes.create({ image: "s3://bucket/image/" });
  const createBody = JSON.parse(String(transport.calls.find((call) => call.path === "/sandboxes")?.init?.body));
  assert.equal("os" in createBody, false);
});

test("delete and close are idempotent", async () => {
  const transport = new FakeTransport();
  const client = new SmolVM({ transport });
  const sandbox = await client.sandboxes.create();
  await Promise.all([sandbox.delete(), sandbox.delete()]);
  await Promise.all([client.close(), client.close()]);
  assert.equal(transport.calls.filter((call) => call.init?.method === "DELETE").length, 1);
  assert.equal(transport.closeCount, 1);
  assert.equal(sandbox.status, "deleted");
});

test("diagnostics excludes bridge credentials", async () => {
  const client = new SmolVM({ transport: new FakeTransport() });
  const report = await client.diagnose();
  assert.equal(report.runtimeVersion, "test");
  assert.equal(JSON.stringify(report).includes("token"), false);
});

test("rejects an incompatible runtime protocol with a stable error", async () => {
  class OldTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path === "/sdk/v1/capabilities") return { protocol_version: 2, capabilities: [] } as T;
      return super.request(path, init);
    }
  }
  const client = new SmolVM({ transport: new OldTransport() });
  await assert.rejects(() => client.sandboxes.create(), (error: unknown) =>
    error instanceof SmolVMError && error.code === "protocol_incompatible",
  );
});

test("recognizes an installed runtime that predates SDK sessions", async () => {
  const directory = await mkdtemp(join(tmpdir(), "smolvm-old-runtime-"));
  const runtime = join(directory, "smolvm");
  await writeFile(runtime, "#!/bin/sh\necho \"Error: No such option '--sdk-session'.\" >&2\nexit 2\n");
  await chmod(runtime, 0o755);
  const transport = new ProcessTransport(runtime, 1_000, 1_000, 1_000, false, () => {});

  try {
    await assert.rejects(() => transport.request("/sdk/v1/capabilities"), (error: unknown) =>
      error instanceof SmolVMError
        && error.code === "protocol_incompatible"
        && error.recoveryCommand === "curl -sSL https://celesto.ai/install.sh | bash",
    );
  } finally {
    await transport.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test("abort confirms sandbox deletion before rejecting", async () => {
  class AbortTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path.endsWith("/exec")) throw new DOMException("aborted", "AbortError");
      return super.request(path, init);
    }
  }
  const transport = new AbortTransport();
  const client = new SmolVM({ transport });
  const sandbox = await client.sandboxes.create();
  await assert.rejects(() => sandbox.exec(["sleep", "60"]), (error: unknown) =>
    error instanceof SmolVMError
      && error.code === "command_aborted"
      && error.actual?.sandboxDeleted === true,
  );
  assert.equal(sandbox.status, "deleted");
});

test("timeout records the server-confirmed sandbox deletion", async () => {
  class TimeoutTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path.endsWith("/exec")) {
        throw new SmolVMError("command_timeout", "timed out and deleted", {
          operation: "POST exec",
          actual: { sandboxDeleted: true },
        });
      }
      return super.request(path, init);
    }
  }
  const client = new SmolVM({ transport: new TimeoutTransport() });
  const sandbox = await client.sandboxes.create();
  await assert.rejects(() => sandbox.exec("sleep 60"), (error: unknown) =>
    error instanceof SmolVMError
      && error.code === "command_timeout"
      && error.actual?.sandboxDeleted === true,
  );
  assert.equal(sandbox.status, "deleted");
});

test("timeout session cleanup invalidates every active sandbox", async () => {
  class TimeoutTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path.endsWith("/exec")) {
        throw new SmolVMError("command_timeout", "timed out without deletion", {
          operation: "POST exec",
          actual: { sandboxDeleted: false },
        });
      }
      return super.request(path, init);
    }
  }
  const transport = new TimeoutTransport();
  const deleted: string[] = [];
  const client = new SmolVM({
    transport,
    onEvent: (event) => {
      if (event.type === "sandbox.deleted") deleted.push(event.sandboxId);
    },
  });
  const first = await client.sandboxes.create();
  const second = await client.sandboxes.create();

  await assert.rejects(() => first.exec("sleep 60"), (error: unknown) =>
    error instanceof SmolVMError
      && error.code === "command_timeout"
      && error.actual?.sessionClosed === true,
  );
  assert.equal(first.status, "deleted");
  assert.equal(second.status, "deleted");
  assert.deepEqual(deleted, [first.id, second.id]);
  await client.close();
  assert.equal(transport.closeCount, 1);
  assert.deepEqual(deleted, [first.id, second.id]);
});

test("abort session cleanup invalidates every active sandbox", async () => {
  class AbortCleanupTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path.endsWith("/exec")) throw new DOMException("aborted", "AbortError");
      if (path.endsWith("/cancel")) throw new Error("cancel confirmation failed");
      return super.request(path, init);
    }
  }
  const transport = new AbortCleanupTransport();
  const deleted: string[] = [];
  const client = new SmolVM({
    transport,
    onEvent: (event) => {
      if (event.type === "sandbox.deleted") deleted.push(event.sandboxId);
    },
  });
  const first = await client.sandboxes.create();
  const second = await client.sandboxes.create();

  await assert.rejects(() => first.exec(["sleep", "60"]), (error: unknown) =>
    error instanceof SmolVMError
      && error.code === "command_aborted"
      && error.actual?.sessionClosed === true,
  );
  assert.equal(first.status, "deleted");
  assert.equal(second.status, "deleted");
  assert.deepEqual(deleted, [first.id, second.id]);
  await client.close();
  assert.equal(transport.closeCount, 1);
  assert.deepEqual(deleted, [first.id, second.id]);
});

test("timeout keeps the sandbox active when session cleanup fails", async () => {
  class CleanupFailureTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path.endsWith("/exec")) {
        throw new SmolVMError("command_timeout", "timed out without deletion", {
          operation: "POST exec",
          actual: { sandboxDeleted: false },
        });
      }
      return super.request(path, init);
    }

    override async close(): Promise<void> {
      this.closeCount += 1;
      if (this.closeCount === 1) throw new Error("bridge still running");
    }
  }
  const transport = new CleanupFailureTransport();
  const client = new SmolVM({ transport });
  const sandbox = await client.sandboxes.create();

  await assert.rejects(() => sandbox.exec("sleep 60"), (error: unknown) =>
    error instanceof SmolVMError
      && error.code === "command_timeout"
      && error.actual?.sandboxDeleted === false
      && error.actual?.sessionClosed === false,
  );
  assert.equal(sandbox.status, "running");

  await sandbox.delete();
  await client.close();
  assert.equal(transport.closeCount, 2);
});

test("a failed capability request can be retried", async () => {
  class RetryTransport extends FakeTransport {
    attempts = 0;

    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (path === "/sdk/v1/capabilities" && this.attempts++ === 0) {
        throw new Error("bridge warming up");
      }
      return super.request(path, init);
    }
  }
  const transport = new RetryTransport();
  const client = new SmolVM({ transport });
  await assert.rejects(() => client.sandboxes.create(), /bridge warming up/);
  await client.sandboxes.create();
  assert.equal(transport.attempts, 2);
});

test("close ends the transport even when sandbox cleanup fails", async () => {
  class CleanupTransport extends FakeTransport {
    override async request<T>(path: string, init?: RequestInit): Promise<T> {
      if (init?.method === "DELETE") throw new Error("busy");
      return super.request(path, init);
    }
  }
  const transport = new CleanupTransport();
  const client = new SmolVM({ transport });
  await client.sandboxes.create();
  await assert.rejects(() => client.close(), (error: unknown) =>
    error instanceof SmolVMError && error.code === "cleanup_failed",
  );
  assert.equal(transport.closeCount, 1);
});
