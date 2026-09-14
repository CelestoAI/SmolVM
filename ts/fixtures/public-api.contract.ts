import { SmolVM, SmolVMError } from "../src/index.js";
import type { BrowserSessionClient, ComputerClient, SandboxClient, SmolVMClient } from "../src/index.js";

const client: SmolVMClient = new SmolVM({ onEvent: (event) => console.log(event.type) });

async function run(sandbox: SandboxClient): Promise<void> {
  const result = await sandbox.exec(["printf", "%s", "hello"], { timeoutMs: 1_000 });
  if (!result.ok) throw new SmolVMError("transport_failed", result.stderr, { operation: "example" });
  await sandbox.files.write("/workspace/result.txt", result.stdout);
}

async function useComputer(computer: ComputerClient): Promise<void> {
  await computer.files.write("/workspace/ready.txt", "ready");
  await computer.exec(["cat", "/workspace/ready.txt"]);
}

async function useBrowser(browser: BrowserSessionClient): Promise<void> {
  await useComputer(browser);
  console.log(browser.cdpUrl, browser.viewerUrl, browser.displayUrl);
}

void client.sandboxes.create().then(run).finally(() => client.close());
void client.browsers.create({ mode: "live" }).then(useBrowser).finally(() => client.close());
