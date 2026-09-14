import { SmolVM } from "@celestoai/smolvm";

async function main() {
  const smolvm = new SmolVM();
  const computer = await smolvm.browsers.create({ mode: "live" });

  try {
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
  console.error(error);
  process.exitCode = 1;
});
