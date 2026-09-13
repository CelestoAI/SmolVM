import assert from "node:assert/strict";
import test from "node:test";
import { API_PROXY_PATTERN } from "../vite.config.js";

test("API proxy does not capture the frontend api.ts module", () => {
  const pattern = new RegExp(API_PROXY_PATTERN);

  assert.equal(pattern.test("/api.ts"), false);
  assert.equal(pattern.test("/api/plans"), true);
  assert.equal(pattern.test("/api"), true);
});
