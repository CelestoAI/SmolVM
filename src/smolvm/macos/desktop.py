# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Safe host-side opening of persisted macOS desktop endpoints."""

from __future__ import annotations

import platform
import subprocess
from urllib.parse import quote

from smolvm.exceptions import SmolVMError
from smolvm.types import DesktopEndpoint


def open_desktop(endpoint: DesktopEndpoint, *, password: str | None = None) -> None:
    """Open a validated loopback VNC URL with macOS Screen Sharing."""
    if platform.system() != "Darwin":
        raise SmolVMError(
            "Desktop viewing is available on macOS hosts; open "
            f"'{endpoint.viewer_url}' with a VNC client on this machine instead."
        )
    viewer_url = endpoint.viewer_url
    command = ["open", viewer_url]
    script: str | None = None
    if password:
        authority = viewer_url.removeprefix("vnc://")
        private_url = f"vnc://:{quote(password, safe='')}@{authority}"
        command = ["osascript", "-"]
        script = f'open location "{private_url}"\n'
    try:
        result = subprocess.run(
            command,
            input=script,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SmolVMError(
            f"Could not open the desktop; open '{endpoint.viewer_url}' in Screen Sharing instead."
        ) from exc
    if result.returncode != 0:
        raise SmolVMError(
            f"Could not open the desktop; open '{endpoint.viewer_url}' in Screen Sharing instead."
        )
