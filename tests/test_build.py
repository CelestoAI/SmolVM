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
from smolvm.build import ImageBuilder
from smolvm.exceptions import ImageError, SmolVMError


def _ok_subprocess_run(
    cmd: list[str], *args: object, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    if cmd[:2] == ["docker", "create"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


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
        first_call = mock_run_command.call_args_list[0]
        second_call = mock_run_command.call_args_list[1]
        third_call = mock_run_command.call_args_list[2]

        assert first_call.args[0][0] == "/usr/local/libexec/smolvm-loopfs-helper"
        assert first_call.args[0][1] == "mount"
        assert second_call.args[0][0] == "/usr/local/libexec/smolvm-loopfs-helper"
        assert second_call.args[0][1] == "extract"
        assert third_call.args[0][0] == "/usr/local/libexec/smolvm-loopfs-helper"
        assert third_call.args[0][1] == "umount"
        assert first_call.kwargs["use_sudo"] is True
        assert second_call.kwargs["use_sudo"] is True
        assert third_call.kwargs["use_sudo"] is True

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
