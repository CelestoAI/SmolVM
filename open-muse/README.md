# OpenMuse

OpenMuse is a chat-based computer coworker that can operate public websites inside a disposable Linux desktop. You chat on the left and watch its computer on the right.

The agent can browse any ordinary public website without a site-specific adapter. Bounded page observation and scrolling run directly. Navigation, clicks, form changes, and keypresses display a one-time approval before they run.

Model credentials stay in the host Node process. The trusted Node broker turns approved structured operations into Playwright commands that run as the unprivileged desktop user inside the VM.

## Run it

Install the packages:

```bash
npm install
```

Create the local environment file:

```bash
cp .env.example .env.local
```

Start the app:

```bash
npm run dev
```

Open [http://127.0.0.1:5174](http://127.0.0.1:5174) and try:

> Open https://example.com and tell me what the page says.

OpenMuse asks you to connect a model provider before the first conversation. You can enter an OpenAI API key in the app, or continue using `OPENAI_API_KEY` from `.env.local`. Saved credentials live in `.open-muse/auth.json` with permissions limited to your operating-system user. They are never returned to the browser after setup.

OpenAI account sign-in is implemented behind a temporary release gate while provider terms are reviewed. For the development smoke only, set `OPEN_MUSE_ENABLE_SUBSCRIPTION_AUTH=1`, restart OpenMuse, and choose **Continue with OpenAI**. The flow opens the provider's secure page and keeps OAuth tokens in the same host-only credential store. Gemini account sign-in is not shown because the pinned Pi harness does not currently expose that capability; it can be added without changing the UI protocol when the provider supports it.

After connecting a provider, choose a model and select **Start using OpenMuse**. Use the **Model** button later to switch models or providers, reconnect an expired account, or disconnect a saved credential. Set `OPEN_MUSE_AUTH_PATH` before starting OpenMuse if you want to keep saved credentials somewhere other than `.open-muse/auth.json`.

To try Markdown extraction after opening a page, ask:

> Give me the raw page data as Markdown.

The first browser action may take a little while while SmolVM downloads and boots a fresh Linux desktop. Later runs reuse the verified image cache. The computer remains warm between chat turns and is deleted when you click **Stop** or stop the server.

OpenMuse saves a small local conversation checkpoint in `.open-muse/state.json`. If the server stops during work, the chat returns in an interrupted state and waits for you to choose **Continue** or **Start over**. Continue always uses a fresh computer and never replays an old approval automatically. Set `OPEN_MUSE_STATE_PATH` to use a different checkpoint file.

This source-checkout app uses `file:../ts` so it can exercise the unreleased computer API. Build that package once before installing if its `dist/` folder is absent:

```bash
(cd ../ts && npm install && npm run build)
```

On macOS, the runtime wrapper downloads the checksum-verified ARM64 Linux guest-agent binary pinned by this checkout. This avoids requiring a Linux cross-linker just to run the example.

## How it works

- Pi is the conversational agent harness.
- `@celestoai/smolvm` creates one ephemeral `linux-desktop` computer with public web access.
- The display and Chromium automation address are grouped separately. OpenMuse streams `computer.display` to the right pane and uses `computer.browser` for Playwright.
- The computer also exposes commands and files. OpenMuse uses `exec()` for its approved runner today; future tools can use `files` without creating another sandbox.
- Pi normally chooses a structured browser operation. OpenMuse builds the corresponding Playwright itself so the model controls arguments, not executable code.
- The broker runs bounded observation, Markdown extraction, and scrolling directly. Active operations create a one-time approval bound to the current page, exact operation arguments, and, when applicable, a short-lived element ref from the latest observation.
- Raw `browser_run` is internal and is not available to the production model. It stays disabled until the guest exposes a browser interface that cannot reach other tabs or the wider browser context.
- New popups stay quarantined until you explicitly adopt them. Each approval is bound to one owned tab and its current page.
- The conversation diagnostics endpoint reports aggregate operation states, durations, and tab counts without including raw URLs, browser arguments, form values, or user text.
- The real browser display is streamed through SmolVM's noVNC viewer, a browser-based remote-display client, into the right pane.
- The trusted Node server keeps the Chrome DevTools Protocol (CDP) automation address and raw VNC remote-display address private. It gives the client only a short-lived path to the noVNC viewer.
- **Take control** pauses Pi across every owned tab and lets you use the browser directly. Return control before sending another chat message.

Structured approvals authorize one browser operation, not a site-specific semantic promise such as an exact cart total. They expire after five minutes and fail if the tab, page, or referenced element changes.

OpenMuse records an approved operation before dispatch and marks it complete only after the browser returns a valid result. A validation or checkpoint failure before dispatch is shown as **Action did not run**. A timeout, crash, malformed result, or other failure after dispatch is shown as **Action outcome unknown**. Continue asks the agent to inspect or ask before acting again; it never retries the uncertain operation automatically.

The initial general-web implementation uses SmolVM's open network mode. Structured navigation rejects local and private literal addresses, but DNS and subresource enforcement still require the approved public-only egress proxy before this example should be treated as a hardened browsing boundary.

See [the browser snapshots, refs, and extraction design](../docs/designs/open-muse-browser-capability.md) for the current browser-tool contract.
See [the approved general-web design](../docs/designs/open-muse-general-web.md) for the staged security model. The original [fixture-store design](../docs/designs/open-muse.md) documents the UI, lifecycle, and takeover flow.
See [the provider-authentication design](../docs/designs/open-muse-provider-authentication.md) for credential storage, model binding, account recovery, and the temporary release gate.

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
npm run test:e2e
```

The browser test uses the real OpenMuse UI with a scripted local API. It starts no VM, calls no model, and makes no internet request. Install its browser once with `npm run test:e2e:install`.

`npm run eval:validate` checks the fixed tool-choice and safety corpus. `npm run eval:artifact` writes a redacted result containing case IDs and a prompt hash, never the prompts themselves. Before a milestone release, validate three separately produced live-model result files with:

```bash
npm run eval:release-gate -- run-1.json run-2.json run-3.json
```

Each live run must pass every safety case, at least 90% of first-tool choices, and at least 80% of tasks. Live runs are deliberately outside pull-request CI because they require an external model and credentials.

Release owners can run the manual **OpenMuse live model release eval** workflow. It evaluates one corpus case at a time against inert browser tools, writes three redacted artifacts, and applies the same gate. The tool stubs record requested tool names and return bounded synthetic observations or approval metadata; they never start a VM, access a website, or execute a browser effect.
