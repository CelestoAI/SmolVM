import { performance } from "node:perf_hooks";
import { SmolVM, type SmolVMEvent } from "../src/index.js";

for (const run of ["cold", "warm"] as const) {
  const started = performance.now();
  const phases: Array<{ type: string; atMs: number }> = [];
  const client = new SmolVM({
    onEvent(event: SmolVMEvent) {
      phases.push({ type: event.type, atMs: Math.round(performance.now() - started) });
    },
  });
  try {
    const sandbox = await client.sandboxes.create({ network: { mode: "off" } });
    await sandbox.files.write("/workspace/input.txt", "hello");
    const result = await sandbox.exec(["cat", "/workspace/input.txt"]);
    if (!result.ok || result.stdout !== "hello") throw new Error("benchmark smoke failed");
  } finally {
    await client.close();
  }
  console.log(JSON.stringify({
    run,
    wallTimeMs: Math.round(performance.now() - started),
    activeDeveloperSteps: 1,
    phases,
  }));
}
