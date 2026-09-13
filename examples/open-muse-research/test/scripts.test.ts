import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import test from "node:test";

const run = promisify(execFile);
const scripts = fileURLToPath(new URL("../server/scripts", import.meta.url));

test("fixed guest scripts calculate and verify a valid four-file packet", async () => {
  const root = await mkdtemp(join(tmpdir(), "open-muse-research-scripts-"));
  const output = join(root, "output");
  await mkdir(output);
  try {
    const source = { id: "S01", url: "https://example.com", finalUrl: "https://example.com", title: "Example", retrievedAt: "2026-09-12T12:00:00Z", contentSha256: "a".repeat(64) };
    await writeFile(join(root, "budget.json"), JSON.stringify([{ category: "food", item: "Meals", quantity: 2, unitCostInr: 1000, sourceId: "S01" }]));
    await run("python3", [join(scripts, "calculate_budget.py"), join(root, "budget.json"), join(output, "budget.csv")]);
    await writeFile(join(output, "sources.json"), JSON.stringify([source]));
    await writeFile(join(output, "brief.md"), "# Brief\nTotal INR 2200. Major assumptions apply.\n\n## Not verified\nAvailability. [source:S01]");
    await writeFile(join(output, "itinerary.md"), "# Day 1\nFood [source:S01]\n# Day 2\nArchitecture [source:S01]\n# Day 3\nWalk [source:S01]");
    const verified = await run("python3", [join(scripts, "verify_packet.py"), output]);
    assert.deepEqual(JSON.parse(verified.stdout), { ok: true, errors: [] });
    const csv = await readFile(join(output, "budget.csv"), "utf8");
    assert.match(csv, /contingency,10% contingency,1,200.00,200.00/);
    const zip = join(root, "packet.zip");
    const encoded = join(root, "packet.zip.b64");
    await run("python3", [join(scripts, "package_packet.py"), output, zip, encoded]);
    assert.ok((await stat(zip)).size > 0);
    assert.deepEqual(Buffer.from(await readFile(encoded, "utf8"), "base64"), await readFile(zip));
    const bounded = await run("python3", ["-c", `import runpy; module=runpy.run_path(${JSON.stringify(join(scripts, "fetch_page.py"))}); body,truncated=module["bounded_body"](b"x"*(module["MAX_BYTES"]+1)); print(len(body), truncated)`]);
    assert.equal(bounded.stdout.trim(), "512000 True");
    const pinned = await run("python3", ["-c", `import runpy; module=runpy.run_path(${JSON.stringify(join(scripts, "fetch_page.py"))}); calls=[]; module["PinnedHTTPSConnection"].__init__.__globals__["socket"].create_connection=lambda address,timeout,source_address: calls.append(address) or object(); connection=module["PinnedHTTPSConnection"]("example.com", "93.184.216.34", timeout=1); connection._create_connection(("example.com",443),1,None); print(calls[0][0])`]);
    assert.equal(pinned.stdout.trim(), "93.184.216.34");
    const redirect = await run("python3", ["-c", `import json,runpy,urllib.request; module=runpy.run_path(${JSON.stringify(join(scripts, "fetch_page.py"))}); calls=[]; module["RedirectHandler"].redirect_request.__globals__["validate_url"]=lambda url: calls.append(url) or "93.184.216.34"; handler=module["RedirectHandler"](); redirected=handler.redirect_request(urllib.request.Request("https://example.com/start"),None,302,"Found",{},"https://redirect.example/path"); module["PinnedHTTPSHandler"].https_open.__globals__["validate_url"]=lambda url: (_ for _ in ()).throw(AssertionError("unexpected second DNS lookup")); module["PinnedHTTPSHandler"].https_open.__globals__["PinnedHTTPSConnection"]=lambda host,pinned_address,**kwargs: {"host":host,"address":pinned_address}; https=module["PinnedHTTPSHandler"](); https.do_open=lambda connection,request: connection(request.host); result=https.https_open(redirected); print(json.dumps({"calls":calls,"result":result}))`]);
    assert.deepEqual(JSON.parse(redirect.stdout), { calls: ["https://redirect.example/path"], result: { host: "redirect.example", address: "93.184.216.34" } });
    await assert.rejects(() => run("python3", ["-c", `import runpy,urllib.request; module=runpy.run_path(${JSON.stringify(join(scripts, "fetch_page.py"))}); module["RedirectHandler"]().redirect_request(urllib.request.Request("https://example.com/start"),None,302,"Found",{},"http://example.com/plain")`]), /Only public HTTPS URLs are allowed/);
    const searchParser = await run("python3", ["-c", `import json,runpy; module=runpy.run_path(${JSON.stringify(join(scripts, "search_web.py"))}); sample='<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Ftrip">Trip guide</a>'; print(json.dumps(module["parse_results"](sample)))`]);
    assert.deepEqual(JSON.parse(searchParser.stdout), [{ title: "Trip guide", url: "https://example.com/trip" }]);
    const lookalikeParser = await run("python3", ["-c", `import json,runpy; module=runpy.run_path(${JSON.stringify(join(scripts, "search_web.py"))}); sample='<a class="result__a" href="https://evilduckduckgo.com/l/?uddg=https%3A%2F%2Finternal.example%2Fsecret">Lookalike</a>'; print(json.dumps(module["parse_results"](sample)))`]);
    assert.deepEqual(JSON.parse(lookalikeParser.stdout), [{
      title: "Lookalike",
      url: "https://evilduckduckgo.com/l/?uddg=https%3A%2F%2Finternal.example%2Fsecret",
    }]);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
