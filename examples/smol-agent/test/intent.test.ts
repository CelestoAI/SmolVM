import assert from "node:assert/strict";
import test from "node:test";
import { groundAddIntent } from "../server/intent.js";

test("grounds a delegated category selection with a price ceiling", () => {
  const grant = groundAddIntent("message-1", "Add the best value wireless headphones under ₹8,000");
  assert.deepEqual(grant?.subject, { categoryId: "wireless-headphones" });
  assert.deepEqual(grant?.variant, { kind: "any" });
  assert.equal(grant?.maxUnitPriceMinor, 800_000);
  assert.equal(grant?.maxQuantity, 1);
});

test("grounds an exact single-variant catalog product", () => {
  const grant = groundAddIntent("message-2", "Please add SoundArc H7");
  assert.deepEqual(grant?.subject, { productId: "soundarc-h7" });
  assert.deepEqual(grant?.variant, { kind: "exact", id: "black" });
  assert.equal(grant?.maxUnitPriceMinor, 699_900);
});

test("does not grant negated, multi-quantity, or browse-only requests", () => {
  assert.equal(groundAddIntent("message-3", "Do not add headphones under ₹8,000"), undefined);
  assert.equal(groundAddIntent("message-4", "Add 2 headphones under ₹8,000"), undefined);
  assert.equal(groundAddIntent("message-5", "Find headphones under ₹8,000"), undefined);
});
