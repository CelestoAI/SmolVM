import { mkdir, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { evalCorpus } from "./corpus.js";
import { runDeterministicEval } from "./runner.js";

const outputIndex = process.argv.indexOf("--output");
const output = outputIndex >= 0 ? process.argv[outputIndex + 1] : undefined;
if (outputIndex >= 0 && !output) throw new Error("Pass a file path after --output.");

const artifact = runDeterministicEval(evalCorpus);
if (artifact.totals.passed !== artifact.totals.cases) {
  for (const item of artifact.cases.filter((entry) => !entry.passed)) console.error(`${item.id}: ${item.reasons.join(" ")}`);
  process.exitCode = 1;
} else {
  console.log(`OpenMuse deterministic eval: ${artifact.totals.passed}/${artifact.totals.cases} passed.`);
}

if (output) {
  const target = resolve(output);
  await mkdir(dirname(target), { recursive: true });
  await writeFile(target, `${JSON.stringify(artifact, null, 2)}\n`, { mode: 0o600 });
  console.log(`Redacted eval artifact written to ${target}.`);
}
