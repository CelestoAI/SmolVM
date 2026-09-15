import { randomUUID } from "node:crypto";
import { chmod, mkdir, open, rename, unlink, writeFile } from "node:fs/promises";
import { dirname } from "node:path";

export async function writePrivateFileAtomically(path: string, contents: string, validateDestination?: () => Promise<void>): Promise<void> {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  await validateDestination?.();
  const temporaryPath = `${path}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporaryPath, contents, { encoding: "utf8", mode: 0o600, flush: true, flag: "wx" });
    await validateDestination?.();
    await rename(temporaryPath, path);
    if (process.platform !== "win32") {
      await chmod(path, 0o600);
      const directory = await open(dirname(path), "r");
      try { await directory.sync(); } finally { await directory.close(); }
    }
  } catch (error) {
    await unlink(temporaryPath).catch(() => undefined);
    throw error;
  }
}
