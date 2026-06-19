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

"""End-to-end browser sandbox smoke test."""

from __future__ import annotations

import json
from contextlib import suppress
from urllib.request import urlopen

import pytest
from _util import BOOT_TIMEOUT, require_backend_available, selected_backend

from smolvm import SmolVM
from smolvm.runtime.backends import BACKEND_QEMU

pytestmark = pytest.mark.e2e


def test_browser_headless_exposes_live_cdp(
    request: pytest.FixtureRequest,
) -> None:
    """Boot a real browser sandbox and confirm Chromium answers on CDP."""
    selected = selected_backend(request.config)
    if selected != "all" and selected != BACKEND_QEMU:
        pytest.skip(f"Browser e2e runs on '{BACKEND_QEMU}' only; this run selected '{selected}'.")
    require_backend_available(
        BACKEND_QEMU,
        request.config,
        sandbox_name="browser-qemu",
    )

    sandbox = SmolVM.browser(
        headless=True,
        backend=BACKEND_QEMU,
        boot_timeout=BOOT_TIMEOUT,
        timeout_minutes=5,
    )
    try:
        cdp_url = sandbox.cdp_url
        assert cdp_url is not None
        assert sandbox.viewer_url is None
        assert sandbox.display_url is None

        with urlopen(f"{cdp_url.rstrip('/')}/json/version", timeout=10) as response:
            assert response.status == 200
            version = json.load(response)

        assert isinstance(version.get("Browser"), str)
        assert version["webSocketDebuggerUrl"].startswith("ws://")
    finally:
        with suppress(Exception):
            sandbox.stop()
