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

"""Shared helpers for the real-KVM end-to-end suite."""

from __future__ import annotations

from pathlib import Path

try:
    from smolvm_core import is_available as _core_available
except (ImportError, OSError):  # pragma: no cover - native extension missing entirely
    _core_available = None

# Boot is fast under KVM, but auto-config may build/download the rootfs and
# base kernel on the first run; give the whole start() a generous budget.
BOOT_TIMEOUT = 180.0


def kvm_ready() -> bool:
    """True when this host can actually run a hardware-accelerated VM."""
    return Path("/dev/kvm").exists() and _core_available is not None and _core_available()
