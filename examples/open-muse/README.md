# OpenMuse

OpenMuse is a chat-based computer coworker that can operate public websites inside a disposable Linux desktop. You chat on the left and watch its computer on the right.

The agent can browse any ordinary public website without a site-specific adapter. Bounded page observation and scrolling run directly. Navigation, clicks, form changes, and keypresses display a one-time approval before they run.

The OpenAI API key stays in the host Node process. The trusted Node broker turns approved structured operations into Playwright commands that run as the unprivileged desktop user inside the VM.

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

The first browser action may take a little while while SmolVM downloads and boots a fresh Linux desktop. Later runs reuse the verified image cache. The computer remains warm between chat turns and is deleted when you click **Stop** or stop the server.

OpenMuse saves a small local conversation checkpoint in `.open-muse/state.json`. If the server stops during work, the chat returns in an interrupted state and waits for you to choose **Continue** or **Start over**. Continue always uses a fresh computer and never replays an old approval automatically. Set `OPEN_MUSE_STATE_PATH` to use a different checkpoint file.

This source-checkout example uses `file:../../ts` so it can exercise the unreleased computer API. Build that package once before installing if its `dist/` folder is absent:

```bash
(cd ../../ts && npm install && npm run build)
```

On macOS, the runtime wrapper downloads the checksum-verified ARM64 Linux guest-agent binary pinned by this checkout. This avoids requiring a Linux cross-linker just to run the example.

## How it works

- Pi is the conversational agent harness.
- `@celestoai/smolvm` creates one ephemeral `linux-desktop` computer with public web access.
- The display and Chromium automation address are grouped separately. OpenMuse streams `computer.display` to the right pane and uses `computer.browser` for Playwright.
- The computer also exposes commands and files. OpenMuse uses `exec()` for its approved runner today; future tools can use `files` without creating another sandbox.
- Pi normally chooses a structured browser operation. OpenMuse builds the corresponding Playwright itself so the model controls arguments, not executable code.
- The broker runs bounded observation and scrolling directly. Active operations create a one-time approval bound to the current page, exact operation arguments, and, when applicable, a uniquely named target.
- Raw `browser_run` is internal and is not available to the production model. It stays disabled until the guest exposes a browser interface that cannot reach other tabs or the wider browser context.
- New popups stay quarantined until you explicitly adopt them. Each approval is bound to one owned tab and its current page.
- The conversation diagnostics endpoint reports aggregate operation states, durations, and tab counts without including raw URLs, browser arguments, form values, or user text.
- The real browser display is streamed through SmolVM's noVNC viewer, a browser-based remote-display client, into the right pane.
- The trusted Node server keeps the Chrome DevTools Protocol (CDP) automation address and raw VNC remote-display address private. It gives the client only a short-lived path to the noVNC viewer.
- **Take control** pauses Pi across every owned tab and lets you use the browser directly. Return control before sending another chat message.

Structured approvals authorize one browser operation, not a site-specific semantic promise such as an exact cart total. They expire after five minutes and fail if the tab, page, or named target changes.

OpenMuse records an approved operation before dispatch and marks it complete only after the browser returns a valid result. A validation or checkpoint failure before dispatch is shown as **Action did not run**. A timeout, crash, malformed result, or other failure after dispatch is shown as **Action outcome unknown**. Continue asks the agent to inspect or ask before acting again; it never retries the uncertain operation automatically.

The initial general-web implementation uses SmolVM's open network mode. Structured navigation rejects local and private literal addresses, but DNS and subresource enforcement still require the approved public-only egress proxy before this example should be treated as a hardened browsing boundary.

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
