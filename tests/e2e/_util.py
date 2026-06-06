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

import platform
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Literal

try:
    from smolvm_core import is_available as _core_available
except (ImportError, OSError):  # pragma: no cover - native extension missing entirely
    _core_available = None

from smolvm.host.manager import HostManager
from smolvm.runtime.backends import BACKEND_FIRECRACKER, BACKEND_QEMU

E2EBackend = Literal["qemu", "firecracker"]
E2ETransport = Literal["ssh", "vsock"]


@dataclass(frozen=True, slots=True)
class E2EVariant:
    """One backend/transport combination exercised by the real-KVM suite."""

    backend: E2EBackend
    transport: E2ETransport

    @property
    def id(self) -> str:
        return f"{self.backend}-{self.transport}"


E2E_BACKENDS: tuple[E2EBackend, ...] = ("qemu", "firecracker")
E2E_VARIANTS: tuple[E2EVariant, ...] = (
    E2EVariant("qemu", "ssh"),
    E2EVariant("qemu", "vsock"),
    E2EVariant("firecracker", "ssh"),
)

# Boot is fast under KVM, but auto-config may build/download the rootfs and
# base kernel on the first run; give the whole start() a generous budget.
BOOT_TIMEOUT = 180.0


def kvm_ready() -> bool:
    """True when this host can actually run a hardware-accelerated VM."""
    return Path("/dev/kvm").exists() and _core_available is not None and _core_available()


def _qemu_binary_available() -> bool:
    arch = platform.machine().lower()
    candidates = (
        ("qemu-system-aarch64",)
        if arch in {"aarch64", "arm64"}
        else ("qemu-system-x86_64", "qemu-system-x86")
    )
    return any(which(candidate) is not None for candidate in candidates)


def backend_unavailable_reasons(backend: E2EBackend) -> list[str]:
    """Return human-readable reasons the backend cannot run on this host."""
    reasons: list[str] = []
    if not kvm_ready():
        reasons.append(
            "requires /dev/kvm and a working smolvm-core native extension "
            "(enable KVM with `sudo modprobe kvm && sudo chmod 666 /dev/kvm`, "
            "then re-run)"
        )

    if backend == BACKEND_QEMU and not _qemu_binary_available():
        reasons.append("requires qemu-system for this host architecture")

    if backend == BACKEND_FIRECRACKER:
        if platform.system() != "Linux":
            reasons.append("Firecracker requires a Linux host")
        if HostManager().find_firecracker() is None:
            reasons.append("requires the firecracker binary on PATH or in ~/.smolvm/bin")

    return reasons
