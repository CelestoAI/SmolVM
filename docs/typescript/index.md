# Use SmolVM from TypeScript

The TypeScript SDK lets a Node.js agent create and control a disposable computer on the same machine. It needs no cloud account, API key, or manually managed server.

> **Alpha:** Node.js 20.4 or newer is supported on Linux x64 and Apple Silicon macOS. The SDK API can be type-checked elsewhere, but the runtime is not yet supported there.

## Set up

Install the SmolVM runtime, the preview package, and a TypeScript runner:

```bash
curl -sSL https://celesto.ai/install.sh | bash
npm install https://github.com/CelestoAI/SmolVM/releases/download/typescript-v0.1.0-preview.1/celestoai-smolvm-0.1.0-preview.1.tgz
npm install --save-dev tsx
```

The SDK starts a private local bridge on first use. Each client gets an isolated sandbox list and a random credential passed through a private process pipe. `close()` removes that client's sandboxes and stops the bridge.

## Manage the lifecycle

Lead with `try/finally` so cleanup also runs after an error:

```ts
import { SmolVM } from "@celestoai/smolvm";

const smolvm = new SmolVM();
const sandbox = await smolvm.sandboxes.create(); // Ubuntu, open network

try {
  const result = await sandbox.exec(["uname", "-a"]);
  console.log(result.stdout);
} finally {
  await smolvm.close();
}
```

`sandbox.delete()` and `smolvm.close()` are idempotent. `Symbol.asyncDispose` is also installed when the running Node version supports it, but the alpha documentation uses `try/finally` for compatibility and clarity.

## Run commands

Pass an argv array when every argument is already known. It avoids adding an extra login-shell wrapper:

```ts
const result = await sandbox.exec(["python3", "-c", "print('hello')"], {
  cwd: "/workspace",
  env: { MODE: "test" },
  timeoutMs: 30_000,
});

if (!result.ok) console.error(result.stderr);
```

A string intentionally uses the guest login shell, so pipes, redirects, and variable expansion work. A command that exits nonzero still resolves with `ok: false`; bridge, lifecycle, timeout, and abort failures throw.

## Read and write files

```ts
await sandbox.files.write("/workspace/input.txt", "hello");
const text = await sandbox.files.read("/workspace/input.txt");

await sandbox.files.upload("./prompt.txt", "/workspace/prompt.txt");
await sandbox.files.download("/workspace/result.json", "./artifacts/result.json");
```

Paths inside the sandbox must be absolute. Uploads send bytes to the bridge rather than exposing a host path. Downloads write a temporary file beside the destination and rename it atomically.

## Limit network access

The default is `{ mode: "open" }`. Security-focused agents can turn access off or allow only IPv4 ranges:

```ts
await smolvm.sandboxes.create({ network: { mode: "off" } });

await smolvm.sandboxes.create({
  network: {
    mode: "restricted",
    allowedCidrs: ["203.0.113.0/24"],
  },
});
```

SmolVM validates the policy before it downloads an image. Backend-specific restrictions still apply; an unavailable combination throws `SmolVMError` with a stable code.

## Cancel a command

```ts
const controller = new AbortController();
setTimeout(() => controller.abort(), 1_000);

await sandbox.exec("sleep 60", { signal: controller.signal });
```

On abort, the SDK asks the bridge to delete the sandbox and waits for confirmation. If that fails, it closes the complete SDK session instead. The resulting `command_aborted` error says which outcome occurred. Runtime timeouts use the same conservative sandbox-deletion rule.

## Handle errors and diagnose setup

```ts
import { SmolVMError } from "@celestoai/smolvm";

try {
  await sandbox.exec(["python3", "job.py"]);
} catch (error) {
  if (error instanceof SmolVMError) {
    console.error(error.code, error.message);
    if (error.recoveryCommand) console.error(error.recoveryCommand);
  }
}

console.log(await smolvm.diagnose());
```

Diagnostics contain versions, platform support, and protocol compatibility. They never include the bridge credential. Construct the client with `{ debug: true }` to retain non-enumerable error causes during local development.

## Use in CI

Package type-checks can run on any Node platform. Tests that boot a VM should run only on a supported, hardware-enabled runner. Always close the client in `finally`, and give the job permission to use the selected virtualization backend.

The [Vercel AI SDK tool example](../../ts/examples/vercel-ai-tool.ts) adds schema validation and explicit command approval. The [versioned API reference](api/0.1/README.md) is generated from the SDK source.

Release candidates can record cold and warm lifecycle timings with `npm run benchmark:release` from the `ts/` directory. It emits one JSON record per run with wall time and event phase timestamps; no report is sent anywhere.

## Current limits

Persistent sandboxes, snapshots, exposed ports, browser control, remote engines, streaming command output, Bun, Deno, and browser runtimes are not part of this alpha. SDK sessions do not list or control sandboxes created by the CLI or another SDK client.
