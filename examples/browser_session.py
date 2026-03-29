#!/usr/bin/env python3

# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Start a disposable SmolVM browser session.

Run:
    python examples/browser_session.py

Optional:
    pip install playwright

If Playwright is installed, this example connects over CDP, opens a page,
and captures a screenshot. The session always exposes its CDP URL and,
when started in live mode, a noVNC live-view URL.
"""

from pathlib import Path

from smolvm import BrowserSession, BrowserSessionConfig, SmolVMError


def main() -> int:
    output_dir = Path("artifacts/browser-example")
    output_dir.mkdir(parents=True, exist_ok=True)

    with BrowserSession(
        BrowserSessionConfig(
            mode="live",
            record_video=True,
            viewport={"width": 1440, "height": 900},
        )
    ) as session:
        print(f"Session: {session.session_id}")
        print(f"VM: {session.vm_id}")
        print(f"CDP URL: {session.cdp_url}")
        print(f"Live URL: {session.live_url}")
        print(f"Artifacts: {session.artifacts_dir}")

        try:
            browser = session.connect_playwright()
        except SmolVMError as error:
            print(f"Skipping Playwright automation: {error}")
            return 0

        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://example.com", wait_until="networkidle")
        screenshot_path = output_dir / "example.png"
        session.screenshot(screenshot_path)
        print(f"Saved screenshot to {screenshot_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
