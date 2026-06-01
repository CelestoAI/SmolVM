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

"""Tests for default kernel boot-arg trims (MICROVM_DIRECT profile)."""

import pytest

from smolvm.runtime.backends import BACKEND_FIRECRACKER, BACKEND_QEMU
from smolvm.runtime.boot_profiles import (
    KernelBootProfile,
    get_boot_profile_spec,
)


def _args(backend: str, arch: str = "x86_64") -> str:
    spec = get_boot_profile_spec(KernelBootProfile.MICROVM_DIRECT)
    return spec.base_boot_args_for_backend(backend, arch)


class TestSafeBootTrims:
    """The safe latency trims must be present, and acpi=off must NOT be."""

    @pytest.mark.parametrize("backend", [BACKEND_QEMU, BACKEND_FIRECRACKER])
    def test_safe_trims_present(self, backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SMOLVM_VERBOSE_BOOT", raising=False)
        args = _args(backend)
        assert "tsc=reliable" in args
        assert "no_timer_check" in args
        assert "init=/init" in args

    @pytest.mark.parametrize("backend", [BACKEND_QEMU, BACKEND_FIRECRACKER])
    def test_acpi_off_is_not_defaulted(self, backend: str) -> None:
        # acpi=off saves ~70ms but isn't universally safe; must stay opt-in.
        assert "acpi=off" not in _args(backend)

    def test_firecracker_keeps_pci_off_and_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SMOLVM_VERBOSE_BOOT", raising=False)
        args = _args(BACKEND_FIRECRACKER)
        assert "pci=off" in args
        assert "root=/dev/vda" in args

    def test_qemu_does_not_get_pci_off(self) -> None:
        # QEMU uses virtio-PCI; pci=off would break it.
        assert "pci=off" not in _args(BACKEND_QEMU)

    def test_quiet_on_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SMOLVM_VERBOSE_BOOT", raising=False)
        assert "quiet" in _args(BACKEND_QEMU)

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
    def test_verbose_boot_drops_quiet(
        self, val: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SMOLVM_VERBOSE_BOOT", val)
        args = _args(BACKEND_QEMU)
        assert "quiet" not in args
        # the rest of the trims stay
        assert "tsc=reliable" in args

    def test_arm64_console_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SMOLVM_VERBOSE_BOOT", raising=False)
        assert "console=ttyAMA0" in _args(BACKEND_QEMU, arch="aarch64")
