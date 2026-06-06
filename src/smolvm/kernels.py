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

"""Public helpers for resolving SmolVM base kernels."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Literal, cast

from smolvm.runtime.backends import resolve_backend
from smolvm.runtime.boot_profiles import to_published_arch

KernelArch = Literal["host", "amd64", "arm64", "x86_64", "aarch64"]
PublishedArch = Literal["amd64", "arm64"]
Vmm = Literal["firecracker", "qemu", "libkrun"]


def _resolve_kernel_arch(arch: KernelArch | str) -> PublishedArch:
    """Resolve a user-facing arch value to SmolVM's published-kernel arch key."""
    raw_arch = platform.machine() if arch == "host" else arch
    return cast(PublishedArch, to_published_arch(raw_arch))


def ensure_base_kernel_for_backend(
    backend: str | None = None,
    arch: KernelArch | str = "host",
    *,
    cache_dir: Path | None = None,
) -> Path:
    """Return the SHA-verified SmolVM base kernel for a backend and arch.

    SmolVM publishes the same kernel build in two container formats. The
    backend determines which one is valid: Firecracker/libkrun use the ELF
    artifact, while QEMU uses the Image/bzImage artifact. This helper keeps
    callers from knowing those release asset details.
    """
    from smolvm.images.published import _kernel_format_for_vmm, ensure_base_kernel

    resolved_backend = resolve_backend(backend)
    resolved_arch = _resolve_kernel_arch(arch)
    vmm = cast(Vmm, resolved_backend)
    kernel_format = _kernel_format_for_vmm(vmm)
    return ensure_base_kernel(
        resolved_arch,
        kernel_format,
        cache_dir=cache_dir,
    )
