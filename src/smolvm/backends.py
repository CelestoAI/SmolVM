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

from smolvm.utils import which

BACKEND_FIRECRACKER = "firecracker"
BACKEND_QEMU = "qemu"
BACKEND_LIBKRUN = "libkrun"
BACKEND_AUTO = "auto"

SUPPORTED_BACKENDS = {BACKEND_FIRECRACKER, BACKEND_QEMU, BACKEND_LIBKRUN}


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
        return BACKEND_FIRECRACKER

    if raw in SUPPORTED_BACKENDS:
        return raw

    supported = ", ".join(sorted((*SUPPORTED_BACKENDS, BACKEND_AUTO)))
    raise ValueError(f"Unsupported backend '{raw}'. Supported values: {supported}")
