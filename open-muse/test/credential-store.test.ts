import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, rm, stat, symlink, utimes, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test, { type TestContext } from "node:test";
import { FileCredentialStore } from "../server/credential-store.js";

async function temporaryCredentials(t: TestContext) {
  const directory = await mkdtemp(join(tmpdir(), "open-muse-auth-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const path = join(directory, "private", "auth.json");
  return { path, store: new FileCredentialStore(path) };
}

test("credentials are stored atomically with private permissions", async (t) => {
  const { path, store } = await temporaryCredentials(t);
  await store.modify("openai", async () => ({ type: "api_key", key: "sk-private" }));

  assert.deepEqual(await store.read("openai"), { type: "api_key", key: "sk-private" });
  assert.deepEqual(await store.list(), [{ providerId: "openai", type: "api_key" }]);
  assert.equal((await stat(path)).mode & 0o777, 0o600);
  assert.equal((await stat(join(path, ".."))).mode & 0o777, 0o700);
  assert.match(await readFile(path, "utf8"), /sk-private/);
});

test("two store instances serialize read-modify-write updates", async (t) => {
  const { path, store } = await temporaryCredentials(t);
  const other = new FileCredentialStore(path);

  await Promise.all([
    store.modify("openai", async () => ({ type: "api_key", key: "one" })),
    other.modify("openai-codex", async () => ({ type: "oauth", access: "access", refresh: "refresh", expires: Date.now() + 60_000 })),
  ]);

  assert.deepEqual(new Set((await store.list()).map((entry) => entry.providerId)), new Set(["openai", "openai-codex"]));
  await other.delete("openai");
  assert.equal(await store.read("openai"), undefined);
});

test("credential files cannot be symbolic links", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "open-muse-auth-link-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const target = join(directory, "target.json");
  const linked = join(directory, "auth.json");
  await symlink(target, linked);

  await assert.rejects(new FileCredentialStore(linked).read("openai"), /cannot be a symbolic link/);
});

test("invalid and oversized credential files report an actionable recovery", async (t) => {
  const { path, store } = await temporaryCredentials(t);
  await mkdir(join(path, ".."), { mode: 0o700 });
  await writeFile(path, "not-json", { encoding: "utf8", mode: 0o600 });
  await assert.rejects(store.read("openai"), (error: unknown) => {
    const message = (error as Error).message;
    return message.includes(`invalid at '${path}'`) && message.includes("Move that file aside");
  });

  await writeFile(path, "x".repeat(1024 * 1024 + 1), { encoding: "utf8", mode: 0o600 });
  await assert.rejects(store.read("openai"), /exceed.*1048576-byte limit/);
});

test("a stale lock from a dead process is removed before writing", async (t) => {
  const { path, store } = await temporaryCredentials(t);
  const lockPath = `${path}.lock`;
  await mkdir(join(path, ".."), { mode: 0o700 });
  await writeFile(lockPath, JSON.stringify({ pid: 2_147_483_647 }), { encoding: "utf8", mode: 0o600 });
  const stale = new Date(Date.now() - 60_000);
  await utimes(lockPath, stale, stale);

  await store.modify("openai", async () => ({ type: "api_key", key: "recovered" }));

  assert.deepEqual(await store.read("openai"), { type: "api_key", key: "recovered" });
  await assert.rejects(stat(lockPath), (error: unknown) => (error as NodeJS.ErrnoException).code === "ENOENT");
});

test("a stale lock with incomplete metadata is removed before writing", async (t) => {
  const { path, store } = await temporaryCredentials(t);
  const lockPath = `${path}.lock`;
  await mkdir(join(path, ".."), { mode: 0o700 });
  await writeFile(lockPath, "", { encoding: "utf8", mode: 0o600 });
  const stale = new Date(Date.now() - 60_000);
  await utimes(lockPath, stale, stale);

  await store.modify("openai", async () => ({ type: "api_key", key: "recovered" }));

  assert.deepEqual(await store.read("openai"), { type: "api_key", key: "recovered" });
  await assert.rejects(stat(lockPath), (error: unknown) => (error as NodeJS.ErrnoException).code === "ENOENT");
});
