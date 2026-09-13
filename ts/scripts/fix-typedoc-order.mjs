import { readFile, writeFile } from "node:fs/promises";

const path = new URL("../../docs/typescript/api/0.1/type-aliases/BrowserSessionStatus.md", import.meta.url);
const markdown = await readFile(path, "utf8");
const pattern = /(#[ ]Type Alias: BrowserSessionStatus\n\n)(> \*\*BrowserSessionStatus\*\*[^\n]+\n\n)([^\n]+\n)/;
const reordered = markdown.replace(pattern, (_match, heading, declaration, comment) => (
  `${heading}${comment.trimEnd()}\n\n${declaration.trimEnd()}\n`
));
if (reordered === markdown) throw new Error("BrowserSessionStatus documentation layout changed; update the TypeDoc postprocessor.");
await writeFile(path, reordered);
