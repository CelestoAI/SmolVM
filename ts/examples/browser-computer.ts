import { SmolVM } from "@celestoai/smolvm";

async function main() {
  const smolvm = new SmolVM();

  try {
    const computer = await smolvm.browsers.create({ mode: "live" });
    await computer.files.write("/workspace/task.txt", "visit example.com");
    console.log({
      sandboxId: computer.sandboxId,
      cdpUrl: computer.cdpUrl,
      viewerUrl: computer.viewerUrl,
      displayUrl: computer.displayUrl,
    });
  } finally {
    await smolvm.close();
  }
}

main().catch((error) => {
  const detail = error instanceof Error ? error.message : "Browser computer failed.";
  console.error(`${detail} Run 'npx tsx examples/browser-computer.ts' to retry.`);
  process.exitCode = 1;
});
