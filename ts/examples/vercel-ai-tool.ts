import { tool } from "ai";
import { z } from "zod";
import type { SandboxClient } from "@celestoai/smolvm";

/** Create a command tool whose every model-proposed invocation needs approval. */
export function createSandboxTools(sandbox: SandboxClient) {
  return {
    runCommand: tool({
      description: "Run an approved command in the disposable SmolVM computer.",
      inputSchema: z.object({
        argv: z.array(z.string()).min(1).describe("Executable followed by its arguments"),
        cwd: z.string().startsWith("/").default("/workspace"),
        timeoutMs: z.number().int().min(1).max(60_000).default(30_000),
      }),
      needsApproval: true,
      execute: async ({ argv, cwd, timeoutMs }) => sandbox.exec(argv, { cwd, timeoutMs }),
    }),
  };
}
