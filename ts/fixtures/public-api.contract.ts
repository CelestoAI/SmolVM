import { SmolVM, SmolVMError } from "../src/index.js";
import type { SandboxClient, SmolVMClient } from "../src/index.js";

const client: SmolVMClient = new SmolVM({ onEvent: (event) => console.log(event.type) });

async function run(sandbox: SandboxClient): Promise<void> {
  const result = await sandbox.exec(["printf", "%s", "hello"], { timeoutMs: 1_000 });
  if (!result.ok) throw new SmolVMError("transport_failed", result.stderr, { operation: "example" });
  await sandbox.files.write("/workspace/result.txt", result.stdout);
}

void client.sandboxes.create().then(run).finally(() => client.close());
