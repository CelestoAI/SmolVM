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

"""Tests for smolvm.runtime.boot_profiles — boot profile resolution."""

from __future__ import annotations

import pytest

from smolvm.runtime.backends import (
    BACKEND_FIRECRACKER,
    BACKEND_LIBKRUN,
    BACKEND_QEMU,
)
from smolvm.runtime.boot_profiles import (
    KernelBootProfile,
    get_boot_profile_spec,
    normalize_arch,
    resolve_bundle_sha256,
    resolve_kernel_url,
)


class TestNormalizeArch:
    def test_x86_64_variants(self) -> None:
        assert normalize_arch("x86_64") == "x86_64"
        assert normalize_arch("X86_64") == "x86_64"
        assert normalize_arch("amd64") == "x86_64"

    def test_aarch64_variants(self) -> None:
        assert normalize_arch("aarch64") == "aarch64"
        assert normalize_arch("arm64") == "aarch64"
        assert normalize_arch("ARM64") == "aarch64"

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported host architecture"):
            normalize_arch("riscv64")


class TestMicrovmDirectProfile:
    """Regression coverage for the flat-file MICROVM_DIRECT profile.

    We're adding a sibling profile (SMOLVM_NATIVE) and don't want to
    silently change the existing one.
    """

    def test_supports_all_three_backends(self) -> None:
        spec = get_boot_profile_spec(KernelBootProfile.MICROVM_DIRECT)
        assert spec.supports_backend(BACKEND_QEMU)
        assert spec.supports_backend(BACKEND_FIRECRACKER)
        assert spec.supports_backend(BACKEND_LIBKRUN)

    def test_is_not_bundle(self) -> None:
        spec = get_boot_profile_spec(KernelBootProfile.MICROVM_DIRECT)
        assert spec.is_bundle is False
        assert spec.bundle_sha256_by_arch is None

    def test_resolve_bundle_sha256_returns_none(self) -> None:
        assert resolve_bundle_sha256(KernelBootProfile.MICROVM_DIRECT, "x86_64") is None


class TestSmolvmNativeProfile:
    """The new profile introduced by PR 1: SmolVM-built kernel bundle."""

    def test_profile_is_registered(self) -> None:
        spec = get_boot_profile_spec(KernelBootProfile.SMOLVM_NATIVE)
        assert spec.profile is KernelBootProfile.SMOLVM_NATIVE

    def test_is_bundle(self) -> None:
        spec = get_boot_profile_spec(KernelBootProfile.SMOLVM_NATIVE)
        assert spec.is_bundle is True
        assert spec.bundle_sha256_by_arch is not None

    def test_boot_mode_is_direct_kernel(self) -> None:
        """The SmolVM kernel boots directly — no initramfs."""
        spec = get_boot_profile_spec(KernelBootProfile.SMOLVM_NATIVE)
        assert spec.boot_mode == "direct_kernel"

    def test_supports_qemu_and_firecracker(self) -> None:
        spec = get_boot_profile_spec(KernelBootProfile.SMOLVM_NATIVE)
        assert spec.supports_backend(BACKEND_QEMU)
        assert spec.supports_backend(BACKEND_FIRECRACKER)

    def test_does_not_support_libkrun(self) -> None:
        """libkrun ships its own embedded kernel — our bundle doesn't apply."""
        spec = get_boot_profile_spec(KernelBootProfile.SMOLVM_NATIVE)
        assert not spec.supports_backend(BACKEND_LIBKRUN)

    def test_url_points_at_github_release_bundle(self) -> None:
        url = resolve_kernel_url(KernelBootProfile.SMOLVM_NATIVE, "x86_64")
        assert url.startswith("https://github.com/celesto-ai/SmolVM/releases/download/")
        assert url.endswith("-x86_64.tar.zst")

    def test_urls_differ_per_arch(self) -> None:
        x86_url = resolve_kernel_url(KernelBootProfile.SMOLVM_NATIVE, "x86_64")
        arm_url = resolve_kernel_url(KernelBootProfile.SMOLVM_NATIVE, "aarch64")
        assert x86_url != arm_url
        assert x86_url.endswith("-x86_64.tar.zst")
        assert arm_url.endswith("-aarch64.tar.zst")

    def test_amd64_and_arm64_aliases_resolve(self) -> None:
        """Host-arch aliases (amd64, arm64) map to the same bundles."""
        assert resolve_kernel_url(KernelBootProfile.SMOLVM_NATIVE, "amd64") == resolve_kernel_url(
            KernelBootProfile.SMOLVM_NATIVE, "x86_64"
        )
        assert resolve_kernel_url(KernelBootProfile.SMOLVM_NATIVE, "arm64") == resolve_kernel_url(
            KernelBootProfile.SMOLVM_NATIVE, "aarch64"
        )

    def test_placeholder_sha256_returns_none(self) -> None:
        """Until the first kernel-v1.0.0 is cut, the sha256 pins are None.

        Callers are expected to refuse to trust a None sha256 — this is
        the safety story for the placeholder release pointer.
        """
        assert resolve_bundle_sha256(KernelBootProfile.SMOLVM_NATIVE, "x86_64") is None
        assert resolve_bundle_sha256(KernelBootProfile.SMOLVM_NATIVE, "aarch64") is None


class TestSmolvmNativeBootArgs:
    """Boot args must match what the Alpine rootfs init expects.

    See src/smolvm/images/builder.py:47 (SSH_BOOT_ARGS) — the SmolVM
    kernel pairs exclusively with that rootfs.
    """

    def test_x86_64_qemu_uses_ttyS0(self) -> None:  # noqa: N802 — ttyS0 is the kernel device name
        spec = get_boot_profile_spec(KernelBootProfile.SMOLVM_NATIVE)
        args = spec.base_boot_args_for_backend(BACKEND_QEMU, "x86_64")
        assert "console=ttyS0" in args

    def test_aarch64_qemu_uses_ttyAMA0(self) -> None:  # noqa: N802 — ttyAMA0 is the kernel device name
        spec = get_boot_profile_spec(KernelBootProfile.SMOLVM_NATIVE)
        args = spec.base_boot_args_for_backend(BACKEND_QEMU, "aarch64")
        assert "console=ttyAMA0" in args

    def test_firecracker_args_match_init_contract(self) -> None:
        spec = get_boot_profile_spec(KernelBootProfile.SMOLVM_NATIVE)
        args = spec.base_boot_args_for_backend(BACKEND_FIRECRACKER, "x86_64")
        assert "root=/dev/vda" in args
        assert "init=/init" in args
        assert "rw" in args

    def test_libkrun_raises(self) -> None:
        """libkrun isn't supported by this profile — asking for args should raise."""
        spec = get_boot_profile_spec(KernelBootProfile.SMOLVM_NATIVE)
        with pytest.raises(ValueError, match="does not support backend"):
            spec.base_boot_args_for_backend(BACKEND_LIBKRUN, "x86_64")
