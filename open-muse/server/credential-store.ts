import { chmod, lstat, mkdir, open, readFile, stat, unlink } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import type {
  AuthOperationOptions,
  Credential,
  CredentialInfo,
  CredentialStore,
} from "@earendil-works/pi-ai";
import { z } from "zod";
import { writePrivateFileAtomically } from "./private-file.js";

const MAX_FILE_BYTES = 1024 * 1024;
const MAX_PROVIDERS = 100;
const LOCK_WAIT_MS = 5_000;
const LOCK_STALE_MS = 30_000;

const credentialSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("api_key"), key: z.string().optional(), env: z.record(z.string(), z.string()).optional() }).passthrough(),
  z.object({ type: z.literal("oauth"), access: z.string(), refresh: z.string(), expires: z.number().finite() }).passthrough(),
]);
const documentSchema = z.object({ fileVersion: z.literal(1), credentials: z.record(z.string().min(1).max(80), credentialSchema) });
type CredentialDocument = z.infer<typeof documentSchema>;

function aborted(options?: AuthOperationOptions): void {
  if (options?.signal?.aborted) throw options.signal.reason ?? new Error("Authentication operation was cancelled.");
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}

export class FileCredentialStore implements CredentialStore {
  readonly path: string;
  private queue: Promise<unknown> = Promise.resolve();

  constructor(path = process.env.OPEN_MUSE_AUTH_PATH ?? ".open-muse/auth.json") {
    this.path = resolve(path);
  }

  async read(providerId: string, options?: AuthOperationOptions): Promise<Credential | undefined> {
    aborted(options);
    await this.queue.catch(() => undefined);
    return (await this.load(options)).credentials[providerId] as Credential | undefined;
  }

  async list(options?: AuthOperationOptions): Promise<readonly CredentialInfo[]> {
    aborted(options);
    await this.queue.catch(() => undefined);
    const document = await this.load(options);
    return Object.entries(document.credentials).map(([providerId, credential]) => ({ providerId, type: credential.type }));
  }

  modify(
    providerId: string,
    fn: (current: Credential | undefined) => Promise<Credential | undefined>,
    options?: AuthOperationOptions,
  ): Promise<Credential | undefined> {
    const work = async () => {
      aborted(options);
      return this.withLock(async () => {
        const document = await this.load(options);
        const current = document.credentials[providerId] as Credential | undefined;
        const next = await fn(current);
        aborted(options);
        if (next === undefined) return current;
        document.credentials[providerId] = credentialSchema.parse(next);
        if (Object.keys(document.credentials).length > MAX_PROVIDERS) throw new Error(`OpenMuse credentials cannot contain more than ${MAX_PROVIDERS} providers.`);
        await this.save(document);
        return next;
      }, options);
    };
    const queued = this.queue.catch(() => undefined).then(work);
    this.queue = queued;
    return queued;
  }

  delete(providerId: string, options?: AuthOperationOptions): Promise<void> {
    const work = async () => {
      aborted(options);
      await this.withLock(async () => {
        const document = await this.load(options);
        delete document.credentials[providerId];
        await this.save(document);
      }, options);
    };
    const queued = this.queue.catch(() => undefined).then(work);
    this.queue = queued;
    return queued;
  }

  private async load(options?: AuthOperationOptions): Promise<CredentialDocument> {
    aborted(options);
    try {
      await this.secureExistingDirectory();
      await this.secureExistingPath();
      const info = await stat(this.path);
      if (info.size > MAX_FILE_BYTES) throw new Error(`OpenMuse credentials exceed the ${MAX_FILE_BYTES}-byte limit at '${this.path}'.`);
      const parsed = documentSchema.parse(JSON.parse(await readFile(this.path, "utf8")));
      if (Object.keys(parsed.credentials).length > MAX_PROVIDERS) throw new Error("too many providers");
      return parsed;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return { fileVersion: 1, credentials: {} };
      if (error instanceof Error && error.message.startsWith("OpenMuse credentials")) throw error;
      throw new Error(`Saved OpenMuse credentials are invalid at '${this.path}'. Move that file aside, then restart OpenMuse.`);
    }
  }

  private async save(document: CredentialDocument): Promise<void> {
    const parsed = documentSchema.parse(document);
    const contents = `${JSON.stringify(parsed, null, 2)}\n`;
    if (Buffer.byteLength(contents) > MAX_FILE_BYTES) throw new Error(`OpenMuse credentials exceed the ${MAX_FILE_BYTES}-byte limit at '${this.path}'.`);
    await writePrivateFileAtomically(this.path, contents, async () => {
      await this.secureDirectory();
      await this.secureExistingPath();
    });
  }

  private async secureExistingDirectory(): Promise<void> {
    try { await this.secureDirectory(); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  }

  private async secureDirectory(): Promise<void> {
    if (process.platform === "win32") return;
    const info = await lstat(dirname(this.path));
    if (info.isSymbolicLink()) throw new Error(`OpenMuse credentials folder cannot be a symbolic link: '${dirname(this.path)}'.`);
    if (typeof info.uid === "number" && info.uid !== process.getuid?.()) throw new Error(`OpenMuse credentials folder is owned by another user: '${dirname(this.path)}'.`);
    if ((info.mode & 0o077) !== 0) await chmod(dirname(this.path), 0o700);
  }

  private async secureExistingPath(): Promise<void> {
    if (process.platform === "win32") return;
    try {
      const info = await lstat(this.path);
      if (info.isSymbolicLink()) throw new Error(`OpenMuse credentials file cannot be a symbolic link: '${this.path}'.`);
      if (typeof info.uid === "number" && info.uid !== process.getuid?.()) throw new Error(`OpenMuse credentials file is owned by another user: '${this.path}'.`);
      if ((info.mode & 0o077) !== 0) await chmod(this.path, 0o600);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
  }

  private async withLock<T>(fn: () => Promise<T>, options?: AuthOperationOptions): Promise<T> {
    const lockPath = `${this.path}.lock`;
    await mkdir(dirname(this.path), { recursive: true, mode: 0o700 });
    await this.secureDirectory();
    const deadline = Date.now() + LOCK_WAIT_MS;
    let handle: Awaited<ReturnType<typeof open>> | undefined;
    while (!handle) {
      aborted(options);
      try {
        handle = await open(lockPath, "wx", 0o600);
        await handle.writeFile(JSON.stringify({ pid: process.pid, createdAt: Date.now() }));
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
        await this.removeStaleLock(lockPath);
        if (Date.now() >= deadline) throw new Error(`OpenMuse credentials are locked at '${lockPath}'. Close the other OpenMuse process, or run 'rm "${lockPath}"' after confirming no process is using it.`);
        await delay(50);
      }
    }
    try { return await fn(); }
    finally {
      await handle.close().catch(() => undefined);
      await unlink(lockPath).catch(() => undefined);
    }
  }

  private async removeStaleLock(lockPath: string): Promise<void> {
    if (process.platform === "win32") return;
    try {
      const info = await stat(lockPath);
      if (Date.now() - info.mtimeMs < LOCK_STALE_MS) return;
      let contents: { pid?: unknown };
      try { contents = JSON.parse(await readFile(lockPath, "utf8")) as { pid?: unknown }; }
      catch { await unlink(lockPath).catch(() => undefined); return; }
      if (typeof contents.pid !== "number") { await unlink(lockPath).catch(() => undefined); return; }
      try { process.kill(contents.pid, 0); }
      catch (error) {
        if ((error as NodeJS.ErrnoException).code === "ESRCH") await unlink(lockPath).catch(() => undefined);
      }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") return;
    }
  }
}
