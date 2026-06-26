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

"""Runtime backend selection helpers for SmolVM."""

from __future__ import annotations

import os
import platform
from pathlib import Path

from smolvm.utils import which  # noqa: F401 — imported for test patching

BACKEND_FIRECRACKER = "firecracker"
BACKEND_QEMU = "qemu"
BACKEND_LIBKRUN = "libkrun"
BACKEND_AUTO = "auto"

SUPPORTED_BACKENDS = {BACKEND_FIRECRACKER, BACKEND_QEMU, BACKEND_LIBKRUN}

# Marker file written by cloud-init inside every SmolVM guest image.
# Presence means we're running inside a SmolVM sandbox, not on the host.
_SMOLVM_GUEST_MARKER = Path("/run/smolvm-guest")


def _running_inside_smolvm_guest() -> bool:
    """Return True if this process is running inside a SmolVM sandbox guest.

    Checks (in order):
    1. ``SMOLVM_NESTED=1`` environment variable (set by profile.d / /etc/environment).
    2. The ``/run/smolvm-guest`` marker file written by cloud-init at boot.
    """
    if os.environ.get("SMOLVM_NESTED") == "1":
        return True
    return _SMOLVM_GUEST_MARKER.exists()


def resolve_backend(requested: str | None = None) -> str:
    """Resolve the effective backend name.

    Resolution order:
    1) Explicit ``requested`` argument.
    2) ``SMOLVM_BACKEND`` environment variable.
    3) Platform-aware default (Darwin -> qemu; others -> firecracker).

    Args:
        requested: Optional backend string.

    Returns:
        Effective backend name.

    Raises:
        ValueError: If backend is unknown.
    """
    raw = (requested or os.environ.get("SMOLVM_BACKEND") or BACKEND_AUTO).strip().lower()

    if raw == BACKEND_AUTO:
        system = platform.system().lower()
        if system == "darwin":
            return BACKEND_QEMU
        if _running_inside_smolvm_guest():
            return BACKEND_QEMU
        return BACKEND_FIRECRACKER

    if raw in SUPPORTED_BACKENDS:
        return raw

    supported = ", ".join(sorted((*SUPPORTED_BACKENDS, BACKEND_AUTO)))
    raise ValueError(f"Unsupported backend '{raw}'. Supported values: {supported}")
