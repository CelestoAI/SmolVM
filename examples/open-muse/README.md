# OpenMuse

OpenMuse is a chat-based computer coworker that can operate public websites inside a disposable SmolVM. You chat on the left and watch its real browser on the right.

The agent can browse any ordinary public website without a site-specific adapter. Every model-proposed Playwright program, including observation and navigation, displays a one-time approval before it runs.

The OpenAI API key stays in the host Node process. Model-written Playwright runs as an unprivileged user inside the browser VM, not in the host process.

## Run it

Install the packages:

```bash
npm install
```

Create the local environment file:

```bash
cp .env.example .env.local
```

Add `OPENAI_API_KEY` to `.env.local`, then start the app:

```bash
npm run dev
```

Open [http://127.0.0.1:5174](http://127.0.0.1:5174) and try:

> Open https://example.com and tell me what the page says.

The first browser action may take a little while because SmolVM boots a fresh browser image. The computer remains warm between chat turns and is deleted when you click **Stop** or stop the server.

This source-checkout example uses `file:../../ts` so it can exercise the unreleased browser-session API. Build that package once before installing if its `dist/` folder is absent:

```bash
(cd ../../ts && npm install && npm run build)
```

On macOS, the runtime wrapper downloads the checksum-verified ARM64 Linux guest-agent binary pinned by this checkout. This avoids requiring a Linux cross-linker just to run the example.

## How it works

- Pi is the conversational agent harness.
- `@celestoai/smolvm` creates a live, ephemeral browser VM with public web access.
- Pi writes a short JavaScript Playwright program for each browser step.
- `browser_run` creates a one-time approval, then executes the approved program through the runner installed inside the VM.
- The real browser display is streamed through SmolVM's noVNC viewer into the right pane.
- **Take control** pauses Pi and lets you use the browser directly. Return control before sending another chat message.

Approval is currently bound to the complete proposed program, not to a site-specific semantic promise such as an exact cart total. Requiring approval for read-only programs is a conservative temporary policy until the runner can enforce the design's finer operation-level boundary.

An approval card says when the proposed program may read the current page after its main browser script stops early. That fallback removes URL query strings, blocks visible text when a route segment is `account`, `auth`, `billing`, `checkout`, `login`, `order`, `payment`, `profile`, `signin`, or `wallet`, redacts email addresses and long payment-like numbers, and returns at most 12,000 visible-text characters. OpenMuse never retries the original website action automatically.

The initial general-web implementation uses SmolVM's open network mode. The approved follow-up design adds a public-only egress proxy that blocks private and metadata destinations before this example should be treated as a hardened browsing boundary.

See [the approved general-web design](../../docs/designs/open-muse-general-web.md) for the staged security model. The original [fixture-store design](../../docs/designs/open-muse.md) documents the UI, lifecycle, and takeover flow.

## Offline fixture mode

The synthetic `shop.smol.test` store remains available for deterministic development and CI. It is generated in memory and never resolves through DNS.

Start the app in fixture mode:

```bash
OPEN_MUSE_FIXTURE_STORE=1 npm run dev
```

Fixture mode disables browser networking and restores the original catalog-specific tools and checkout-review approval.

## Checks

```bash
npm run check
```
