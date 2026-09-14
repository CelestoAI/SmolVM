# Browser sandboxes

A browser sandbox runs Chromium in a disposable sandbox. Use it when an agent needs a real browser without using your desktop profile.

## Start and open a browser

```bash
smolvm browser start --session-id research --live
smolvm browser open research
```

The first command starts Chromium and prints connection details. The second opens its browser view on your machine.

List running browser sandboxes when you need to find a session:

```bash
smolvm browser list
```

Stop one when you are finished:

```bash
smolvm browser stop research
```

## Keep a browser profile

A normal browser sandbox is temporary. Use a persistent profile when you deliberately want later sessions to reuse browser state:

```bash
smolvm browser start --profile-mode persistent --profile-id work
```

Use `--live` when you need the interactive display URLs, and `--record-video` when you need a recording. Browser downloads are enabled unless you pass `--no-downloads`.

## Use it from Python

Install Playwright on your machine before using the Python browser connection:

```bash
pip install playwright
```

Then connect to Chromium running inside the sandbox:

```python
from smolvm import SmolVM

with SmolVM.browser() as browser:
    remote_browser = browser.connect_playwright()
    page = remote_browser.contexts[0].new_page()
    page.goto("https://example.com")
```

## Use it from TypeScript

The source checkout contains the browser-session API planned for the next TypeScript preview. Until that preview is published, install the local `ts/` package rather than `0.1.0-preview.1`.

Install the browser automation client before running the example:

```bash
npm install playwright-core
```

```ts
import { chromium } from "playwright-core";
import { SmolVM } from "@celestoai/smolvm";

const smolvm = new SmolVM();
const session = await smolvm.browsers.create({
  mode: "live",
  profile: { mode: "ephemeral" },
});

try {
  await session.files.write("/workspace/task.txt", "visit example.com");
  const browser = await chromium.connectOverCDP(session.cdpUrl);
  const context = browser.contexts()[0];
  if (!context) throw new Error("Browser context is unavailable.");
  const page = context.pages()[0] ?? await context.newPage();
  await page.goto("https://example.com");
  console.log({
    sandboxId: session.sandboxId,
    cdpUrl: session.cdpUrl,
    viewerUrl: session.viewerUrl,
    displayUrl: session.displayUrl,
  });
  await browser.close();
} finally {
  await smolvm.close();
}
```

The returned browser session is also the sandbox computer. Use `session.exec()` for commands and `session.files` for file transfer. In live mode, `viewerUrl` opens the complete graphical display in a web browser and `displayUrl` connects a VNC client or computer-use agent. The display is a minimal Openbox desktop containing Chromium, not a full GNOME or XFCE installation.

The automation, viewer, and display endpoints are loopback-only. Keep them in the trusted Node process rather than sending them to browser JavaScript or a remote client.

## Implementation notes

Python browser sessions, profile IDs, local viewer endpoints, artifacts, and Playwright connections are implemented in [`src/smolvm/browser.py`](../../src/smolvm/browser.py). The TypeScript session wrapper is in [`ts/src/browser-session.ts`](../../ts/src/browser-session.ts), and the private bridge routes are in [`src/smolvm/server/app.py`](../../src/smolvm/server/app.py). Public configuration types are in [`src/smolvm/types.py`](../../src/smolvm/types.py) and [`ts/src/types.ts`](../../ts/src/types.ts), with coverage in [`tests/e2e/test_browser.py`](../../tests/e2e/test_browser.py), [`tests/integration/test_server.py`](../../tests/integration/test_server.py), and [`ts/test/sdk.test.ts`](../../ts/test/sdk.test.ts).
