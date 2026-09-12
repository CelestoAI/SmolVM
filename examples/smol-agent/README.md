# Smol Agent

Smol Agent is a conversational computer coworker built with Pi and SmolVM. You chat on the left and watch the agent use a real disposable browser on the right. The first demo is deliberately offline: it shops in a local fake store, can add one item only when your message authorizes that exact action, and asks before opening checkout review.

Nothing in this milestone can place an order, use a real account, reach the public internet, run shell commands, or read host files. The OpenAI API key stays in the host Node process and is never copied into the VM.

## Run it

From this directory:

```bash
cp .env.example .env.local
# Add OPENAI_API_KEY to .env.local
npm install
npm run dev
```

Open [http://127.0.0.1:5174](http://127.0.0.1:5174). Try:

> Add the best value wireless headphones under ₹8,000

The first browser action may take a little while because SmolVM boots a fresh browser image. The computer remains warm between chat turns and is deleted when you click **Stop**, stop the server, or close the process.

This source-checkout example uses `file:../../ts` so it can exercise the unreleased browser-session API. Build that package once before installing if its `dist/` folder is absent:

```bash
(cd ../../ts && npm install && npm run build)
```

On macOS, the runtime wrapper uses the ARM64 Linux guest-agent binary pinned and checksum-verified by this SmolVM checkout. This avoids requiring a system-wide Linux cross-linker just to run the example.

Before publishing the example, replace the file dependency with an immutable `@celestoai/smolvm` release containing `SmolVM.browsers.create()`.

## Architecture

- Pi is the host-side agent harness.
- `@celestoai/smolvm` creates a live, ephemeral, network-off browser session.
- Playwright connects from the host over the private CDP endpoint.
- A deterministic action broker checks short-lived grants grounded from the user's message.
- The browser renders through SmolVM's real noVNC display and a same-origin viewer proxy.
- **Take control** pauses Pi before removing the input shield. **Return control** gives the browser back to the agent.

The model only receives semantic browser tools. It never receives raw Playwright, CDP, JavaScript evaluation, a shell, or filesystem access. See [the approved design](../../docs/designs/smol-agent.md) for the threat model and later authenticated-browser milestone.

## Checks

```bash
npm run check
```
