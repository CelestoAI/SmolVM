import assert from "node:assert/strict";
import test from "node:test";
import { z } from "zod";
import { PublicError } from "../server/errors.js";
import { _test as serverTest } from "../server/index.js";

test("labels request validation errors without masking internal Zod errors", () => {
  assert.throws(
    () => serverTest.parseRequest(z.object({ goal: z.string().min(10) }), { goal: "short" }),
    (error) => error instanceof PublicError && error.status === 400 && /goal and constraints/.test(error.message),
  );

  const internal = new z.ZodError([]);
  const publicError = serverTest.toHttpError(internal);
  assert.equal(publicError.status, 500);
  assert.equal(publicError.message, "Open Muse could not finish this research packet.");
});

test("mutation requests require JSON and reject cross-site browser origins", () => {
  assert.throws(
    () => serverTest.assertMutationRequest({ headers: { host: "127.0.0.1:5173", "content-type": "text/plain" } }, true),
    (error: unknown) => (error as { status?: number }).status === 415,
  );
  assert.throws(
    () => serverTest.assertMutationRequest({ headers: {
      host: "127.0.0.1:5173",
      origin: "https://attacker.example",
      "content-type": "application/json",
    } }, true),
    (error: unknown) => (error as { status?: number }).status === 403,
  );
  assert.doesNotThrow(() => serverTest.assertMutationRequest({ headers: {
    host: "127.0.0.1:5173",
    origin: "http://127.0.0.1:5173",
    "content-type": "application/json; charset=utf-8",
    "sec-fetch-site": "same-origin",
  } }, true));
});
