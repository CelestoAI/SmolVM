import { createReadStream } from "node:fs";
import { mkdir, readFile, rename, stat, unlink, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { basename, dirname, join } from "node:path";
import { SmolVMError } from "./errors.js";
import type { SandboxFiles, SmolVMTransport } from "./types.js";

/** @internal */
export class RemoteFiles implements SandboxFiles {
  constructor(
    private readonly transport: SmolVMTransport,
    private readonly resourcePath: string,
    private readonly resourceName: string,
    private readonly assertAvailable: () => void,
  ) {}

  async read(path: string): Promise<string> {
    return new TextDecoder().decode(await this.readBytes(this.absolutePath(path, "files.read('/workspace/file')")));
  }

  async write(path: string, content: string | Uint8Array): Promise<void> {
    const bytes = typeof content === "string" ? new TextEncoder().encode(content) : content;
    await this.writeBytes(this.absolutePath(path, "files.write('/workspace/file', content)"), bytes);
  }

  async upload(localPath: string, targetPath: string): Promise<void> {
    const path = this.absolutePath(targetPath, "files.upload(localPath, '/workspace/file')");
    this.assertAvailable();
    if (!this.transport.requestStream) {
      await this.write(path, await readFile(localPath));
      return;
    }
    const metadata = await stat(localPath);
    await this.transport.requestStream(
      this.fileUrl(path),
      createReadStream(localPath),
      metadata.size,
    );
  }

  async download(sourcePath: string, localPath: string): Promise<void> {
    const bytes = await this.readBytes(
      this.absolutePath(sourcePath, "files.download('/workspace/file', localPath)"),
    );
    const parent = dirname(localPath);
    await mkdir(parent, { recursive: true });
    const temporary = join(parent, `.${basename(localPath)}.smolvm-${randomUUID()}.tmp`);
    try {
      await writeFile(temporary, bytes);
      await rename(temporary, localPath);
    } finally {
      await unlink(temporary).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== "ENOENT") throw error;
      });
    }
  }

  private fileUrl(path: string): string {
    return `${this.resourcePath}/files?path=${encodeURIComponent(path)}`;
  }

  private absolutePath(path: string, recoveryCall: string): string {
    if (!path.startsWith("/")) {
      throw new SmolVMError(
        "invalid_path",
        `${this.resourceName} paths must be absolute; call ${recoveryCall}.`,
        { operation: "files.path", actual: { path } },
      );
    }
    return path;
  }

  private async readBytes(path: string): Promise<Uint8Array> {
    this.assertAvailable();
    return this.transport.requestBytes(this.fileUrl(path));
  }

  private async writeBytes(path: string, content: Uint8Array): Promise<void> {
    this.assertAvailable();
    const body = new ArrayBuffer(content.byteLength);
    new Uint8Array(body).set(content);
    await this.transport.request<void>(this.fileUrl(path), {
      method: "PUT",
      headers: { "content-type": "application/octet-stream" },
      body,
    });
  }
}
