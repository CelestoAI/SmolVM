import { defineConfig } from "@hey-api/openapi-ts";

// Generates the low-level typed client from the server's OpenAPI spec.
// Regenerate with `npm run generate` after the spec changes
// (dump it via the Python helper, then run this).
export default defineConfig({
  input: "./openapi.json",
  output: "./src/client",
  plugins: ["@hey-api/client-fetch"],
});
