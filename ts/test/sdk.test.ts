import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
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
    if (path === "/sdk/v1/capabilities") return { protocol_version: 1, capabilities: ["sandbox.create", "sandbox.delete", "sandbox.exec", "files.read", "files.write", "events"] } as T;
    if (path === "/sdk/v1/diagnostics") return { protocol_version: 1, runtime_version: "test", python_version: "3.13", platform: "darwin-arm64", supported: true, problems: [] } as T;
    if (path === "/sandboxes") return { id: "sbx-test", status: "running" } as T;
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

test("sandbox creation timeout closes its SDK session", async () => {
  const server = createServer(() => { /* Keep the request open until its deadline. */ });
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

  try {
    await assert.rejects(
      () => transport.request("/sandboxes", { method: "POST" }),
      (error: unknown) => error instanceof SmolVMError
        && error.code === "sandbox_create_failed"
        && error.actual?.createTimeoutMs === 20
        && error.actual?.sessionClosed === true,
    );
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
  await assert.rejects(() => sandbox.files.read("relative.txt"), (error: unknown) => error instanceof SmolVMError && error.code === "invalid_path");
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
