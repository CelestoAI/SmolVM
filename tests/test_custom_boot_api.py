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

"""Tests for public custom-image boot and kernel APIs."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.images import DirectKernelBoot
from smolvm.kernels import ensure_base_kernel_for_backend


def _tokens(args: str) -> set[str]:
    return set(args.split())


class TestDirectKernelBoot:
    """DirectKernelBoot centralizes backend-specific kernel args."""

    def test_firecracker_render_includes_pci_off_and_safe_trims(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SMOLVM_VERBOSE_BOOT", raising=False)
        args = DirectKernelBoot(root="/dev/vda", init="/init").render(
            backend="firecracker",
            arch="amd64",
        )

        assert args == (
            "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw "
            "init=/init tsc=reliable no_timer_check quiet"
        )

    def test_qemu_render_excludes_pci_off_but_keeps_root(self) -> None:
        args = DirectKernelBoot(root="/dev/vda", init="/init", quiet=False).render(
            backend="qemu",
            arch="amd64",
        )

        tokens = _tokens(args)
        assert "console=ttyS0" in tokens
        assert "pci=off" not in tokens
        assert "root=/dev/vda" in tokens
        assert "rw" in tokens
        assert "init=/init" in tokens
        assert "quiet" not in tokens

    def test_qemu_arm64_uses_pl011_console(self) -> None:
        args = DirectKernelBoot(quiet=False).render(backend="qemu", arch="arm64")
        assert args.startswith("console=ttyAMA0 ")

    def test_libkrun_uses_qemu_style_args(self) -> None:
        args = DirectKernelBoot(quiet=False).render(backend="libkrun", arch="amd64")
        tokens = _tokens(args)
        assert "console=ttyS0" in tokens
        assert "pci=off" not in tokens
        assert "root=/dev/vda" in tokens

    def test_verbose_boot_env_drops_default_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SMOLVM_VERBOSE_BOOT", "1")
        args = DirectKernelBoot().render(backend="firecracker", arch="amd64")
        assert "quiet" not in _tokens(args)
        assert "tsc=reliable" in _tokens(args)

    def test_console_none_and_extra_args(self) -> None:
        args = DirectKernelBoot(
            console="none",
            init=None,
            rw=False,
            quiet=False,
            extra_args=("foo=bar", "acpi=off"),
        ).render(backend="qemu", arch="amd64")

        tokens = _tokens(args)
        assert not any(token.startswith("console=") for token in tokens)
        assert "ro" in tokens
        assert not any(token.startswith("init=") for token in tokens)
        assert "foo=bar" in tokens
        assert "acpi=off" in tokens

    def test_extra_args_must_be_single_tokens(self) -> None:
        with pytest.raises(ValueError, match="one kernel argument token"):
            DirectKernelBoot(extra_args=("foo=bar baz=qux",))


class TestEnsureBaseKernelForBackend:
    """Kernel resolution hides published asset format details."""

    @patch("smolvm.images.published.ensure_base_kernel")
    def test_qemu_selects_image_kernel(self, mock_ensure: MagicMock) -> None:
        kernel = Path("sentinels/vmlinux.image")
        mock_ensure.return_value = kernel

        assert ensure_base_kernel_for_backend("qemu", arch="amd64") == kernel
        mock_ensure.assert_called_once_with("amd64", "image", cache_dir=None)

    @patch("smolvm.images.published.ensure_base_kernel")
    def test_firecracker_selects_elf_kernel(self, mock_ensure: MagicMock) -> None:
        kernel = Path("sentinels/vmlinux.elf")
        mock_ensure.return_value = kernel

        assert ensure_base_kernel_for_backend("firecracker", arch="arm64") == kernel
        mock_ensure.assert_called_once_with("arm64", "elf", cache_dir=None)

    @patch("smolvm.images.published.ensure_base_kernel")
    def test_libkrun_selects_elf_kernel(self, mock_ensure: MagicMock) -> None:
        kernel = Path("sentinels/vmlinux.elf")
        mock_ensure.return_value = kernel

        assert ensure_base_kernel_for_backend("libkrun", arch="x86_64") == kernel
        mock_ensure.assert_called_once_with("amd64", "elf", cache_dir=None)

    @patch("smolvm.images.published.ensure_base_kernel")
    def test_host_arch_is_normalized(
        self,
        mock_ensure: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        kernel = Path("sentinels/vmlinux.image")
        mock_ensure.return_value = kernel
        monkeypatch.setattr("smolvm.kernels.platform.machine", lambda: "aarch64")

        assert ensure_base_kernel_for_backend("qemu", arch="host") == kernel
        mock_ensure.assert_called_once_with("arm64", "image", cache_dir=None)
