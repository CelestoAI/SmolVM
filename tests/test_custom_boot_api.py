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

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import ImageError
from smolvm.images import BootImage, DirectKernelBoot, DockerRootfsBuilder, FirmwareBoot
from smolvm.kernels import ensure_base_kernel_for_backend


def _tokens(args: str) -> set[str]:
    return set(args.split())


def _empty_file(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


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


class TestBootImage:
    """BootImage describes bootable artifacts without launching a VM."""

    def test_direct_kernel_image_renders_profile_args(self, tmp_path: Path) -> None:
        rootfs = _empty_file(tmp_path, "rootfs.ext4")
        kernel = _empty_file(tmp_path, "vmlinux.elf")

        image = BootImage(
            name=" custom-image ",
            rootfs_path=rootfs,
            rootfs_format="raw-ext4",
            kernel_path=kernel,
            boot=DirectKernelBoot(quiet=False),
            backend="firecracker",
            arch="amd64",
        )

        assert image.name == "custom-image"
        assert image.boot_mode == "direct_kernel"
        assert image.render_boot_args() == (
            "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw "
            "init=/init tsc=reliable no_timer_check"
        )

    def test_direct_kernel_image_accepts_explicit_boot_args(self, tmp_path: Path) -> None:
        rootfs = _empty_file(tmp_path, "rootfs.ext4")

        image = BootImage(
            name="explicit-args",
            rootfs_path=rootfs,
            rootfs_format="raw-ext4",
            boot_args=" console=ttyS0 root=/dev/vda rw init=/init ",
        )

        assert image.boot_mode == "direct_kernel"
        assert image.render_boot_args() == "console=ttyS0 root=/dev/vda rw init=/init"

    def test_direct_kernel_image_can_omit_kernel_for_later_resolution(self, tmp_path: Path) -> None:
        rootfs = _empty_file(tmp_path, "rootfs.ext4")

        image = BootImage(
            name="needs-kernel-resolution",
            rootfs_path=rootfs,
            rootfs_format="raw-ext4",
            boot=DirectKernelBoot(quiet=False),
            backend="qemu",
            arch="amd64",
        )

        assert image.kernel_path is None
        assert "pci=off" not in _tokens(image.render_boot_args())

    def test_boot_and_boot_args_are_mutually_exclusive(self, tmp_path: Path) -> None:
        rootfs = _empty_file(tmp_path, "rootfs.ext4")

        with pytest.raises(ValueError, match="mutually exclusive"):
            BootImage(
                name="ambiguous",
                rootfs_path=rootfs,
                rootfs_format="raw-ext4",
                boot=DirectKernelBoot(),
                boot_args="console=ttyS0 root=/dev/vda rw",
            )

    def test_direct_kernel_image_needs_boot_metadata(self, tmp_path: Path) -> None:
        rootfs = _empty_file(tmp_path, "rootfs.ext4")

        with pytest.raises(ValueError, match="need boot or boot_args"):
            BootImage(name="missing-boot", rootfs_path=rootfs, rootfs_format="raw-ext4")

    def test_firmware_image_has_empty_boot_args(self, tmp_path: Path) -> None:
        rootfs = _empty_file(tmp_path, "rootfs.qcow2")

        image = BootImage(
            name="firmware-image",
            rootfs_path=rootfs,
            rootfs_format="qcow2",
            boot=FirmwareBoot(),
            backend="qemu",
        )

        assert image.boot_mode == "firmware"
        assert image.render_boot_args() == ""

    def test_firmware_image_rejects_direct_kernel_fields(self, tmp_path: Path) -> None:
        rootfs = _empty_file(tmp_path, "rootfs.qcow2")
        kernel = _empty_file(tmp_path, "vmlinux.image")

        with pytest.raises(ValueError, match="must not set kernel_path"):
            BootImage(
                name="bad-firmware",
                rootfs_path=rootfs,
                rootfs_format="qcow2",
                kernel_path=kernel,
                boot=FirmwareBoot(),
                backend="qemu",
            )

    def test_firmware_image_rejects_non_qemu_backend(self, tmp_path: Path) -> None:
        rootfs = _empty_file(tmp_path, "rootfs.qcow2")

        with pytest.raises(ValueError, match="backend='qemu'"):
            BootImage(
                name="bad-firmware-backend",
                rootfs_path=rootfs,
                rootfs_format="qcow2",
                boot=FirmwareBoot(),
                backend="firecracker",
            )

    def test_path_fields_must_exist(self, tmp_path: Path) -> None:
        missing_rootfs = tmp_path / "missing.ext4"

        with pytest.raises(ValueError, match="Path does not exist"):
            BootImage(
                name="missing-path",
                rootfs_path=missing_rootfs,
                rootfs_format="raw-ext4",
                boot_args="console=ttyS0 root=/dev/vda rw",
            )

    def test_extra_fields_are_rejected(self, tmp_path: Path) -> None:
        rootfs = _empty_file(tmp_path, "rootfs.ext4")

        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            BootImage(
                name="extra-field",
                rootfs_path=rootfs,
                rootfs_format="raw-ext4",
                boot_args="console=ttyS0 root=/dev/vda rw",
                unexpected=True,
            )

    def test_top_level_exports(self) -> None:
        from smolvm import BootImage as TopLevelBootImage
        from smolvm import FirmwareBoot as TopLevelFirmwareBoot
        from smolvm.images import BootImage as ImagesBootImage
        from smolvm.images import FirmwareBoot as ImagesFirmwareBoot

        assert TopLevelBootImage is BootImage
        assert TopLevelFirmwareBoot is FirmwareBoot
        assert ImagesBootImage is BootImage
        assert ImagesFirmwareBoot is FirmwareBoot


class TestDockerRootfsBuilder:
    """DockerRootfsBuilder builds a generic rootfs and returns BootImage metadata."""

    @staticmethod
    def _fake_build_rootfs(**kwargs: object) -> None:
        rootfs_path = kwargs["rootfs_path"]
        assert isinstance(rootfs_path, Path)
        rootfs_path.write_bytes(b"ext4")

    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_build_boot_image_builds_rootfs_and_returns_boot_image(
        self,
        _mock_check_docker: MagicMock,
        mock_kernel: MagicMock,
        tmp_path: Path,
    ) -> None:
        cache_dir = tmp_path / "cache"
        kernel = _empty_file(tmp_path, "kernel/vmlinux.image")
        mock_kernel.return_value = kernel

        builder = DockerRootfsBuilder(
            name="celesto-scratch",
            dockerfile="FROM scratch\nCOPY init /init\n",
            context={"init": "#!/bin/sh\n"},
            rootfs_size_mb=64,
            cache_dir=cache_dir,
            fingerprint_inputs={"template": "scratch"},
        )
        with patch.object(
            DockerRootfsBuilder,
            "_build_rootfs",
            side_effect=self._fake_build_rootfs,
        ):
            image = builder.build_boot_image(
                backend="qemu",
                arch="amd64",
                boot=DirectKernelBoot(quiet=False),
            )

        assert image.name == "celesto-scratch"
        assert image.rootfs_format == "raw-ext4"
        assert image.rootfs_path.is_file()
        assert image.kernel_path == kernel
        assert image.backend == "qemu"
        assert image.arch == "amd64"
        assert "pci=off" not in _tokens(image.render_boot_args())
        assert image.rootfs_path.parent.parent.name == "celesto-scratch"
        mock_kernel.assert_called_once_with("qemu", arch="amd64", cache_dir=cache_dir)

    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_build_boot_image_uses_ext4_suffixed_temp_rootfs(
        self,
        _mock_check_docker: MagicMock,
        mock_kernel: MagicMock,
        tmp_path: Path,
    ) -> None:
        cache_dir = tmp_path / "cache"
        kernel = _empty_file(tmp_path, "kernel/vmlinux.image")
        mock_kernel.return_value = kernel
        temp_paths: list[Path] = []

        def fake_build_rootfs(**kwargs: object) -> None:
            rootfs_path = kwargs["rootfs_path"]
            assert isinstance(rootfs_path, Path)
            temp_paths.append(rootfs_path)
            rootfs_path.write_bytes(b"ext4")

        builder = DockerRootfsBuilder(
            name="loopfs-temp",
            dockerfile="FROM scratch\n",
            rootfs_size_mb=64,
            cache_dir=cache_dir,
        )
        with patch.object(
            DockerRootfsBuilder,
            "_build_rootfs",
            side_effect=fake_build_rootfs,
        ):
            image = builder.build_boot_image(
                backend="qemu",
                arch="amd64",
                boot=DirectKernelBoot(quiet=False),
            )

        assert image.rootfs_path.name == "rootfs.ext4"
        assert len(temp_paths) == 1
        temp_rootfs = temp_paths[0]
        assert temp_rootfs.name == ".rootfs.tmp.ext4"
        assert temp_rootfs.suffix == ".ext4"
        assert temp_rootfs != image.rootfs_path
        assert not temp_rootfs.exists()

    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_ensure_remains_backward_compatible(
        self,
        _mock_check_docker: MagicMock,
        mock_kernel: MagicMock,
        tmp_path: Path,
    ) -> None:
        cache_dir = tmp_path / "cache"
        kernel = _empty_file(tmp_path, "kernel/vmlinux.image")
        mock_kernel.return_value = kernel
        builder = DockerRootfsBuilder(
            name="ensure-alias",
            dockerfile="FROM scratch\n",
            rootfs_size_mb=64,
            cache_dir=cache_dir,
        )
        with patch.object(
            DockerRootfsBuilder,
            "_build_rootfs",
            side_effect=self._fake_build_rootfs,
        ):
            image = builder.ensure(
                backend="qemu",
                arch="amd64",
                boot=DirectKernelBoot(quiet=False),
            )

        assert image.name == "ensure-alias"
        assert image.rootfs_path.name == "rootfs.ext4"

    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_cached_rootfs_is_reused_across_backends(
        self,
        _mock_check_docker: MagicMock,
        mock_kernel: MagicMock,
        tmp_path: Path,
    ) -> None:
        cache_dir = tmp_path / "cache"
        qemu_kernel = _empty_file(tmp_path, "kernel/vmlinux.image")
        fc_kernel = _empty_file(tmp_path, "kernel/vmlinux.elf")
        mock_kernel.side_effect = [qemu_kernel, fc_kernel]
        builder = DockerRootfsBuilder(
            name="shared-rootfs",
            dockerfile="FROM scratch\n",
            rootfs_size_mb=64,
            cache_dir=cache_dir,
        )

        with patch.object(
            DockerRootfsBuilder,
            "_build_rootfs",
            side_effect=self._fake_build_rootfs,
        ) as mock_build:
            qemu_image = builder.build_boot_image(
                backend="qemu",
                arch="amd64",
                boot=DirectKernelBoot(quiet=False),
            )
            firecracker_image = builder.build_boot_image(
                backend="firecracker",
                arch="amd64",
                boot=DirectKernelBoot(quiet=False),
            )

        assert qemu_image.rootfs_path == firecracker_image.rootfs_path
        assert qemu_image.kernel_path == qemu_kernel
        assert firecracker_image.kernel_path == fc_kernel
        assert mock_build.call_count == 1

    def test_context_paths_must_be_relative_and_safe(self, tmp_path: Path) -> None:
        builder = DockerRootfsBuilder(
            name="bad-context",
            dockerfile="FROM scratch\n",
            context={"../init": "#!/bin/sh\n"},
            cache_dir=tmp_path / "cache",
        )

        with pytest.raises(ValueError, match="relative and safe"):
            builder.build_boot_image(
                backend="qemu",
                arch="amd64",
                boot_args="console=ttyS0 root=/dev/vda rw",
            )

    def test_context_rejects_dockerfile_case_variants(self, tmp_path: Path) -> None:
        builder = DockerRootfsBuilder(
            name="bad-context",
            dockerfile="FROM scratch\n",
            context={"nested/dockerfile": "FROM scratch\n"},
            cache_dir=tmp_path / "cache",
        )

        with pytest.raises(ValueError, match="Dockerfile"):
            builder.build_boot_image(
                backend="qemu",
                arch="amd64",
                boot_args="console=ttyS0 root=/dev/vda rw",
            )

    def test_missing_context_file_fails_before_docker(self, tmp_path: Path) -> None:
        builder = DockerRootfsBuilder(
            name="missing-context",
            dockerfile="FROM scratch\n",
            context={"init": tmp_path / "missing-init"},
            cache_dir=tmp_path / "cache",
        )

        with pytest.raises(ImageError, match="Build context file is missing"):
            builder.build_boot_image(
                backend="qemu",
                arch="amd64",
                boot_args="console=ttyS0 root=/dev/vda rw",
            )

    def test_docker_build_failure_raises_image_error(self, tmp_path: Path) -> None:
        builder = DockerRootfsBuilder(
            name="build-fails",
            dockerfile="FROM scratch\n",
            cache_dir=tmp_path / "cache",
        )

        with (
            patch(
                "smolvm.images.builder.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    7,
                    ["docker", "build"],
                    stderr=b"build failed",
                ),
            ),
            pytest.raises(ImageError, match="Docker build failed.*exit code 7.*build failed"),
        ):
            builder._build_rootfs(
                helper=MagicMock(),
                rootfs_path=tmp_path / "rootfs.ext4",
                docker_platform="linux/amd64",
                context_files={},
                docker_tag="test-image",
            )

    def test_docker_create_failure_raises_image_error(self, tmp_path: Path) -> None:
        builder = DockerRootfsBuilder(
            name="create-fails",
            dockerfile="FROM scratch\n",
            cache_dir=tmp_path / "cache",
        )

        with (
            patch(
                "smolvm.images.builder.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(["docker", "build"], 0),
                    subprocess.CalledProcessError(
                        8,
                        ["docker", "create"],
                        stderr="create failed",
                    ),
                ],
            ),
            pytest.raises(ImageError, match="Docker create failed.*exit code 8.*create failed"),
        ):
            builder._build_rootfs(
                helper=MagicMock(),
                rootfs_path=tmp_path / "rootfs.ext4",
                docker_platform="linux/amd64",
                context_files={},
                docker_tag="test-image",
            )

    def test_docker_export_failure_raises_image_error(self, tmp_path: Path) -> None:
        builder = DockerRootfsBuilder(
            name="export-fails",
            dockerfile="FROM scratch\n",
            cache_dir=tmp_path / "cache",
        )

        with (
            patch(
                "smolvm.images.builder.subprocess.run",
                side_effect=[
                    subprocess.CompletedProcess(["docker", "build"], 0),
                    subprocess.CompletedProcess(["docker", "create"], 0, stdout="container\n"),
                    subprocess.CalledProcessError(
                        9,
                        ["docker", "export"],
                        output="export failed",
                    ),
                    subprocess.CompletedProcess(["docker", "rm"], 0),
                ],
            ),
            pytest.raises(ImageError, match="Docker export failed.*exit code 9.*export failed"),
        ):
            builder._build_rootfs(
                helper=MagicMock(),
                rootfs_path=tmp_path / "rootfs.ext4",
                docker_platform="linux/amd64",
                context_files={},
                docker_tag="test-image",
            )

    def test_top_level_builder_export(self) -> None:
        from smolvm import DockerRootfsBuilder as TopLevelDockerRootfsBuilder
        from smolvm.images import DockerRootfsBuilder as ImagesDockerRootfsBuilder

        assert TopLevelDockerRootfsBuilder is DockerRootfsBuilder
        assert ImagesDockerRootfsBuilder is DockerRootfsBuilder


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
    def test_libkrun_selects_elf_kernel_on_linux(
        self, mock_ensure: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # libkrun on Linux uses KVM and accepts the ELF kernel like Firecracker.
        monkeypatch.setattr("platform.system", lambda: "Linux")
        kernel = Path("sentinels/vmlinux.elf")
        mock_ensure.return_value = kernel

        assert ensure_base_kernel_for_backend("libkrun", arch="x86_64") == kernel
        mock_ensure.assert_called_once_with("amd64", "elf", cache_dir=None)

    @patch("smolvm.images.published.ensure_base_kernel")
    def test_libkrun_selects_image_kernel_on_darwin(
        self, mock_ensure: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # libkrun on macOS uses Hypervisor.framework, which rejects ELF kernels
        # and requires the ARM64 boot Image format instead.
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        kernel = Path("sentinels/vmlinux.image")
        mock_ensure.return_value = kernel

        assert ensure_base_kernel_for_backend("libkrun", arch="arm64") == kernel
        mock_ensure.assert_called_once_with("arm64", "image", cache_dir=None)

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
