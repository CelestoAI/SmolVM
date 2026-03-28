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

"""Tests for SmolVM image builder module."""

import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.boot_profiles import KernelBootProfile, resolve_kernel_url
from smolvm.build import ImageBuilder
from smolvm.exceptions import ImageError, SmolVMError


def _ok_subprocess_run(
    cmd: list[str], *args: object, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    if cmd[:2] == ["docker", "create"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class TestDockerDiagnostics:
    """Tests for Docker availability diagnostics."""

    def test_docker_requirement_error_when_docker_missing(self, tmp_path: Path) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        with patch("smolvm.build.shutil.which", return_value=None):
            error = builder.docker_requirement_error()

        assert str(error) == (
            "Docker is required to build images. "
            "Install Docker Desktop (macOS) or docker.io (Linux)."
        )

    @patch("smolvm.build.subprocess.run")
    def test_docker_requirement_error_when_daemon_unreachable(
        self, mock_subprocess_run: MagicMock, tmp_path: Path
    ) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")
        mock_subprocess_run.side_effect = subprocess.CalledProcessError(
            1,
            ["docker", "info"],
            stderr=(
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                "Is the docker daemon running?"
            ),
        )

        with patch("smolvm.build.shutil.which", return_value="/usr/bin/docker"):
            error = builder.docker_requirement_error()

        assert "could not reach the Docker daemon" in str(error)
        assert "Start Docker Desktop or the Docker service" in str(error)
        assert "Cannot connect to the Docker daemon" in str(error)

    @patch("smolvm.build.subprocess.run")
    def test_docker_requirement_error_when_socket_permission_denied(
        self, mock_subprocess_run: MagicMock, tmp_path: Path
    ) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")
        mock_subprocess_run.side_effect = subprocess.CalledProcessError(
            1,
            ["docker", "info"],
            stderr=(
                "error during connect: permission denied while trying to connect "
                "to the Docker daemon socket at unix:///var/run/docker.sock"
            ),
        )

        with patch("smolvm.build.shutil.which", return_value="/usr/bin/docker"):
            error = builder.docker_requirement_error()

        assert "cannot access the Docker daemon socket" in str(error)
        assert "docker.sock" in str(error)


class TestImageBuilderLoopFs:
    """Tests for image builder loopfs helper integration."""

    def test_run_loopfs_missing_helper_raises(self, tmp_path: Path) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        with (
            patch.object(ImageBuilder, "_loopfs_helper_path", return_value=None),
            pytest.raises(ImageError, match="--configure-runtime"),
        ):
            builder._run_loopfs("mount", Path("/tmp/rootfs.ext4"), Path("/tmp/mnt"))

    @patch("smolvm.build.run_command")
    def test_run_loopfs_maps_runtime_error(
        self, mock_run_command: MagicMock, tmp_path: Path
    ) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")
        mock_run_command.side_effect = SmolVMError("sudo: a password is required")

        with (
            patch.object(
                ImageBuilder,
                "_loopfs_helper_path",
                return_value=Path("/usr/local/libexec/smolvm-loopfs-helper"),
            ),
            pytest.raises(ImageError, match="--configure-runtime"),
        ):
            builder._run_loopfs("mount", Path("/tmp/rootfs.ext4"), Path("/tmp/mnt"))

    @patch("smolvm.build.subprocess.run")
    @patch("smolvm.build.run_command")
    def test_do_build_uses_loopfs_helper(
        self, mock_run_command: MagicMock, mock_subprocess_run: MagicMock, tmp_path: Path
    ) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")
        mock_subprocess_run.side_effect = _ok_subprocess_run
        mock_run_command.return_value = subprocess.CompletedProcess(
            args=["sudo", "-n", "/usr/local/libexec/smolvm-loopfs-helper"],
            returncode=0,
            stdout="",
            stderr="",
        )

        image_dir = tmp_path / "image"
        image_dir.mkdir()
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        with (
            patch.object(
                ImageBuilder,
                "_loopfs_helper_path",
                return_value=Path("/usr/local/libexec/smolvm-loopfs-helper"),
            ),
            patch.object(ImageBuilder, "_download_kernel"),
        ):
            builder._do_build(
                name="demo",
                dockerfile_content="FROM scratch\n",
                init_script="#!/bin/sh\n",
                image_dir=image_dir,
                kernel_path=kernel_path,
                rootfs_path=rootfs_path,
                rootfs_size_mb=8,
            )

        assert mock_run_command.call_count == 3


class TestBrowserImageBuilder:
    """Tests for browser image builder entrypoints."""

    @patch.object(ImageBuilder, "_host_arch_key", return_value="x86_64")
    @patch.object(ImageBuilder, "check_docker", return_value=True)
    @patch.object(ImageBuilder, "_do_build")
    def test_build_browser_rootfs_wires_guest_helpers(
        self,
        mock_do_build: MagicMock,
        _mock_check_docker: MagicMock,
        _mock_host_arch_key: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Browser rootfs builds should include Chromium and guest helper scripts."""
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        def _fake_do_build(
            name: str,
            dockerfile_content: str,
            init_script: str,
            image_dir: Path,
            kernel_path: Path,
            rootfs_path: Path,
            rootfs_size_mb: int,
            **kwargs: object,
        ) -> None:
            assert name == "browser-chromium"
            assert "chromium" in dockerfile_content
            assert "websockify" in dockerfile_content
            assert "x11vnc" in dockerfile_content
            assert init_script.startswith("#!/bin/sh")
            assert rootfs_size_mb == 4096
            assert kwargs["kernel_url"] == resolve_kernel_url(
                KernelBootProfile.MICROVM_DIRECT,
                "x86_64",
            )
            assert kwargs["fingerprint_data"]["kernel_profile"] == "microvm_direct"
            assert kwargs["fingerprint_data"]["image_type"] == "browser-chromium-v3"
            helper_script = kwargs["extra_files"]["smolvm-browser-session"]
            assert "127.0.0.1:5900" in helper_script
            assert "--remote-debugging-address=0.0.0.0" in helper_script
            kernel_path.touch()
            rootfs_path.touch()

        mock_do_build.side_effect = _fake_do_build

        kernel, rootfs = builder.build_browser_rootfs(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test"
        )

        assert kernel.exists()
        assert rootfs.exists()
        extra_files = mock_do_build.call_args.kwargs["extra_files"]
        assert "smolvm-browser-session" in extra_files
        assert "smolvm-browser-wait-port" in extra_files

    @patch.object(ImageBuilder, "_host_arch_key", return_value="x86_64")
    @patch.object(ImageBuilder, "check_docker", return_value=True)
    @patch.object(ImageBuilder, "_do_build")
    def test_build_browser_rootfs_rebuilds_when_kernel_profile_changes(
        self,
        mock_do_build: MagicMock,
        _mock_check_docker: MagicMock,
        _mock_host_arch_key: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Browser image cache keys should change when the internal boot profile changes."""
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        def _fake_do_build(
            name: str,
            dockerfile_content: str,
            init_script: str,
            image_dir: Path,
            kernel_path: Path,
            rootfs_path: Path,
            rootfs_size_mb: int,
            **kwargs: object,
        ) -> None:
            del name, dockerfile_content, init_script, rootfs_size_mb
            kernel_path.touch()
            rootfs_path.touch()
            builder._write_fingerprint(image_dir, kwargs["fingerprint_data"])

        mock_do_build.side_effect = _fake_do_build

        builder.build_browser_rootfs("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test")
        builder.build_browser_rootfs("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test")
        builder.build_browser_rootfs(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test",
            kernel_profile=KernelBootProfile.QEMU_DESKTOP_INITRAMFS,
        )

        assert mock_do_build.call_count == 2

    @patch("smolvm.build.subprocess.run")
    @patch("smolvm.build.run_command")
    def test_do_build_uses_docker_fallback_when_loopfs_missing(
        self, mock_run_command: MagicMock, mock_subprocess_run: MagicMock, tmp_path: Path
    ) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        def _subprocess_side_effect(
            cmd: list[str], *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["docker", "create"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")

            if cmd[:2] == ["docker", "export"]:
                tar_index = cmd.index("-o") + 1
                tar_path = Path(cmd[tar_index])
                with tarfile.open(tar_path, "w"):
                    pass
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            if cmd[:2] == ["docker", "run"]:
                volumes = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-v"]
                out_host = Path(volumes[1].split(":", 1)[0])
                (out_host / "rootfs.ext4").write_bytes(b"ext4")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        mock_subprocess_run.side_effect = _subprocess_side_effect

        image_dir = tmp_path / "image"
        image_dir.mkdir()
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        with (
            patch.object(ImageBuilder, "_loopfs_helper_path", return_value=None),
            patch.object(
                ImageBuilder,
                "_kernel_url_for_host",
                return_value="https://example.invalid/vmlinux",
            ),
            patch.object(ImageBuilder, "_download_kernel"),
        ):
            builder._do_build(
                name="demo",
                dockerfile_content="FROM scratch\n",
                init_script="#!/bin/sh\n",
                image_dir=image_dir,
                kernel_path=kernel_path,
                rootfs_path=rootfs_path,
                rootfs_size_mb=8,
            )

        assert mock_run_command.call_count == 0
        docker_run_calls = [
            call
            for call in mock_subprocess_run.call_args_list
            if call.args[0][:2] == ["docker", "run"]
        ]
        assert len(docker_run_calls) == 1
        assert rootfs_path.exists()

    @patch("smolvm.build.subprocess.run")
    @patch("smolvm.build.run_command")
    def test_do_build_preserves_tar_error_when_unmount_fails(
        self, mock_run_command: MagicMock, mock_subprocess_run: MagicMock, tmp_path: Path
    ) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        def _subprocess_side_effect(
            cmd: list[str], *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["docker", "create"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        def _run_command_side_effect(
            cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if len(cmd) > 1 and cmd[1] == "extract":
                raise SmolVMError("extract failed")
            if len(cmd) > 1 and cmd[1] == "umount":
                raise SmolVMError("umount failed")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        mock_subprocess_run.side_effect = _subprocess_side_effect
        mock_run_command.side_effect = _run_command_side_effect

        image_dir = tmp_path / "image"
        image_dir.mkdir()
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        with (
            patch.object(
                ImageBuilder,
                "_loopfs_helper_path",
                return_value=Path("/usr/local/libexec/smolvm-loopfs-helper"),
            ),
            pytest.raises(ImageError, match="extract"),
        ):
            builder._do_build(
                name="demo",
                dockerfile_content="FROM scratch\n",
                init_script="#!/bin/sh\n",
                image_dir=image_dir,
                kernel_path=kernel_path,
                rootfs_path=rootfs_path,
                rootfs_size_mb=8,
            )

        assert mock_run_command.call_count == 3
