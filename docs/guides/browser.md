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

## Implementation notes

Browser sessions, profile IDs, local viewer endpoints, artifacts, and Playwright connections are implemented in [`src/smolvm/browser.py`](../../src/smolvm/browser.py). Their public configuration types are in [`src/smolvm/types.py`](../../src/smolvm/types.py), with coverage in [`tests/test_browser.py`](../../tests/test_browser.py).
