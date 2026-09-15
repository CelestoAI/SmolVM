import assert from "node:assert/strict";
import test from "node:test";
import { productionPolicyDescriptor } from "../server/agent.js";

test("production policy exposes only structured browser tools", () => {
  const policy = productionPolicyDescriptor();
  const names = policy.tools.map((tool) => tool.name);

  assert.deepEqual(names, [
    "browser_observe",
    "browser_extract",
    "browser_scroll",
    "browser_navigate",
    "browser_click",
    "browser_fill",
    "browser_select",
    "browser_keypress",
  ]);
  assert.equal(names.includes("browser_run"), false);
  assert.match(policy.systemPrompt, /Arbitrary browser programs are unavailable/);
  assert.doesNotMatch(policy.systemPrompt, /Use browser_run/);
});
