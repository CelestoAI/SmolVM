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
