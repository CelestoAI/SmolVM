import { mkdir, writeFile } from "node:fs/promises";

await mkdir(new URL("../dist/cjs/", import.meta.url), { recursive: true });
await writeFile(new URL("../dist/cjs/package.json", import.meta.url), `${JSON.stringify({ type: "commonjs" })}\n`);
