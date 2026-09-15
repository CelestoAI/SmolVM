import { readFile } from "node:fs/promises";
import { assertReleaseThresholds, evalArtifactSchema } from "./runner.js";

const paths = process.argv.slice(2);
if (paths.length !== 3) throw new Error("Usage: npm run eval:release-gate -- run-1.json run-2.json run-3.json");
const artifacts = await Promise.all(paths.map(async (path) => evalArtifactSchema.parse(JSON.parse(await readFile(path, "utf8")))));
assertReleaseThresholds(artifacts);
console.log("OpenMuse live model eval gate passed: 3/3 runs met every release threshold.");
