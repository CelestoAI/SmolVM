# @celestoai/smolvm

Run TypeScript agent code in a disposable computer on your own machine. No cloud account or API key is required.

This package is an alpha for Node.js 20.4 or newer on Linux x64 and Apple Silicon macOS. Install the SmolVM runtime first; the SDK starts its private local bridge automatically.

```bash
curl -sSL https://celesto.ai/install.sh | bash
npm install https://github.com/CelestoAI/SmolVM/releases/download/typescript-v0.1.0-preview.1/celestoai-smolvm-0.1.0-preview.1.tgz
npm install --save-dev tsx
```

```ts
import { SmolVM } from "@celestoai/smolvm";

async function main() {
  const smolvm = new SmolVM();
  const sandbox = await smolvm.sandboxes.create({ network: { mode: "off" } });

  try {
    await sandbox.files.write("/workspace/input.txt", "hello");
    const result = await sandbox.exec(["cat", "/workspace/input.txt"]);
    console.log(result.stdout);
  } finally {
    await smolvm.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
```

See the TypeScript guide in the source repository for lifecycle, files, network policy, cancellation, diagnostics, and CI examples.
