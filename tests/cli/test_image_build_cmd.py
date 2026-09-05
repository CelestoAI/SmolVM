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

"""Tests for `smolvm image build` (Docker is always mocked)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cli.main import main


def _fake_build_rootfs(**kwargs: Any) -> None:
    rootfs_path = kwargs["rootfs_path"]
    assert isinstance(rootfs_path, Path)
    rootfs_path.write_bytes(b"ext4")


@pytest.fixture
def build_ctx(tmp_path: Path) -> Path:
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\nCOPY init /init\n")
    (ctx / "init").write_text("#!/bin/sh\n")
    return ctx


class TestImageBuild:
    @patch("smolvm.images.builder.DockerRootfsBuilder._build_rootfs")
    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_build_json_payload(
        self,
        _mock_docker: MagicMock,
        mock_kernel: MagicMock,
        mock_build: MagicMock,
        build_ctx: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        kernel = tmp_path / "vmlinux.image"
        kernel.write_bytes(b"k")
        mock_kernel.return_value = kernel
        mock_build.side_effect = _fake_build_rootfs

        ret = main(
            [
                "image",
                "build",
                "-t",
                "myimg",
                str(build_ctx),
                "--backend",
                "qemu",
                "--arch",
                "amd64",
                "--image-dir",
                str(tmp_path / "cache"),
                "--json",
            ]
        )

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["command"] == "image.build"
        data = payload["data"]
        assert data["name"] == "myimg"
        assert data["cached"] is False
        assert data["arch"] == "amd64"
        assert Path(data["rootfs_path"]).is_file()
        assert Path(data["rootfs_path"]).parent.name == data["fingerprint"]
        assert "root=/dev/vda" in data["boot_args"]
        # The build landed in the custom namespace of the chosen image dir.
        assert str(tmp_path / "cache" / "custom" / "myimg") in data["rootfs_path"]

    @patch("smolvm.images.builder.DockerRootfsBuilder._build_rootfs")
    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_second_build_reports_cached(
        self,
        _mock_docker: MagicMock,
        mock_kernel: MagicMock,
        mock_build: MagicMock,
        build_ctx: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        kernel = tmp_path / "vmlinux.image"
        kernel.write_bytes(b"k")
        mock_kernel.return_value = kernel
        mock_build.side_effect = _fake_build_rootfs

        args = [
            "image",
            "build",
            "-t",
            "myimg",
            str(build_ctx),
            "--backend",
            "qemu",
            "--arch",
            "amd64",
            "--image-dir",
            str(tmp_path / "cache"),
            "--json",
        ]
        assert main(args) == 0
        capsys.readouterr()
        assert main(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["data"]["cached"] is True
        assert mock_build.call_count == 1  # cache hit skipped Docker entirely

    @patch("smolvm.images.builder.DockerRootfsBuilder._build_rootfs")
    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_built_image_appears_in_list_and_rm(
        self,
        _mock_docker: MagicMock,
        mock_kernel: MagicMock,
        mock_build: MagicMock,
        build_ctx: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        kernel = tmp_path / "vmlinux.image"
        kernel.write_bytes(b"k")
        mock_kernel.return_value = kernel
        mock_build.side_effect = _fake_build_rootfs
        cache = str(tmp_path / "cache")

        assert (
            main(
                [
                    "image",
                    "build",
                    "-t",
                    "myimg",
                    str(build_ctx),
                    "--backend",
                    "qemu",
                    "--arch",
                    "amd64",
                    "--image-dir",
                    cache,
                    "--json",
                ]
            )
            == 0
        )
        capsys.readouterr()

        assert main(["image", "list", "--image-dir", cache, "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)["data"]["images"]
        assert any(r["kind"] == "custom" and r["name"] == "custom/myimg" for r in rows)

        assert main(["image", "rm", "custom/myimg", "--image-dir", cache, "--json"]) == 0
        capsys.readouterr()
        assert not (tmp_path / "cache" / "custom" / "myimg").exists()

    def test_missing_dockerfile(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        ret = main(["image", "build", "-t", "x", str(empty), "--json"])

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"
        assert "No Dockerfile" in payload["error"]["message"]
        assert "-f option" in payload["error"]["recovery"]

    def test_bad_build_arg(self, build_ctx: Path, capsys: pytest.CaptureFixture) -> None:
        ret = main(
            ["image", "build", "-t", "x", str(build_ctx), "--build-arg", "NOEQUALS", "--json"]
        )

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert "KEY=VALUE" in payload["error"]["recovery"]

    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=False)
    def test_docker_unavailable_error_envelope(
        self,
        _mock_docker: MagicMock,
        build_ctx: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        ret = main(
            [
                "image",
                "build",
                "-t",
                "x",
                str(build_ctx),
                "--image-dir",
                str(tmp_path / "cache"),
                "--json",
            ]
        )

        assert ret == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert "Docker" in payload["error"]["message"]
        assert "smolvm image build" in payload["error"]["recovery"]

    @patch("smolvm.images.builder.DockerRootfsBuilder._build_rootfs")
    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_nested_dockerfiles_are_skipped(
        self,
        _mock_docker: MagicMock,
        mock_kernel: MagicMock,
        mock_build: MagicMock,
        build_ctx: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The builder reserves the name Dockerfile; nested ones must not
        abort the build."""
        nested = build_ctx / "sub"
        nested.mkdir()
        (nested / "Dockerfile").write_text("FROM other\n")
        kernel = tmp_path / "vmlinux.image"
        kernel.write_bytes(b"k")
        mock_kernel.return_value = kernel
        mock_build.side_effect = _fake_build_rootfs

        ret = main(
            [
                "image",
                "build",
                "-t",
                "myimg",
                str(build_ctx),
                "--backend",
                "qemu",
                "--arch",
                "amd64",
                "--image-dir",
                str(tmp_path / "cache"),
                "--json",
            ]
        )

        assert ret == 0
        json.loads(capsys.readouterr().out)

    @patch("smolvm.images.builder.DockerRootfsBuilder._build_rootfs")
    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_human_output_names_sdk_boot_path(
        self,
        _mock_docker: MagicMock,
        mock_kernel: MagicMock,
        mock_build: MagicMock,
        build_ctx: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        kernel = tmp_path / "vmlinux.image"
        kernel.write_bytes(b"k")
        mock_kernel.return_value = kernel
        mock_build.side_effect = _fake_build_rootfs

        ret = main(
            [
                "image",
                "build",
                "-t",
                "myimg",
                str(build_ctx),
                "--backend",
                "qemu",
                "--arch",
                "amd64",
                "--image-dir",
                str(tmp_path / "cache"),
            ]
        )

        assert ret == 0
        out = capsys.readouterr().out
        assert "SmolVM.from_image" in out
        assert "smolvm image rm custom/myimg" in out


class TestBuildValidation:
    def test_tag_cannot_escape_custom_namespace(
        self, build_ctx: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """-t '..' would write outside custom/ (regression)."""
        ret = main(
            [
                "image",
                "build",
                "-t",
                "..",
                str(build_ctx),
                "--image-dir",
                str(tmp_path / "cache"),
                "--json",
            ]
        )

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"
        assert not (tmp_path / "cache").exists()

    def test_invalid_init_gets_clean_envelope(
        self, build_ctx: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A bad --init used to escape as a raw ValueError traceback
        (regression)."""
        ret = main(
            [
                "image",
                "build",
                "-t",
                "x",
                str(build_ctx),
                "--init",
                "bad init",
                "--image-dir",
                str(tmp_path / "cache"),
                "--json",
            ]
        )

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_input"

    def test_missing_context_folder(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """A typo'd context path must not silently build with no files
        (regression)."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        ret = main(
            [
                "image",
                "build",
                "-t",
                "x",
                "-f",
                str(dockerfile),
                str(tmp_path / "nope"),
                "--json",
            ]
        )

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert "not a folder" in payload["error"]["message"]

    @patch("smolvm.images.builder.DockerRootfsBuilder._build_rootfs")
    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_skipped_dockerfiles_reach_json_warnings(
        self,
        _mock_docker: MagicMock,
        mock_kernel: MagicMock,
        mock_build: MagicMock,
        build_ctx: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """JSON callers must see the same skip warnings humans do
        (regression)."""
        nested = build_ctx / "sub"
        nested.mkdir()
        (nested / "Dockerfile").write_text("FROM other\n")
        kernel = tmp_path / "vmlinux.image"
        kernel.write_bytes(b"k")
        mock_kernel.return_value = kernel
        mock_build.side_effect = _fake_build_rootfs

        ret = main(
            [
                "image",
                "build",
                "-t",
                "myimg",
                str(build_ctx),
                "--backend",
                "qemu",
                "--arch",
                "amd64",
                "--image-dir",
                str(tmp_path / "cache"),
                "--json",
            ]
        )

        assert ret == 0
        payload = json.loads(capsys.readouterr().out)
        assert any("sub/Dockerfile" in w for w in payload["data"]["warnings"])


class TestUpstreamReviewRegressions:
    def test_leading_dot_tag_rejected(
        self, build_ctx: Path, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A dot-prefixed tag would build an image invisible to image list
        (regression)."""
        ret = main(
            [
                "image",
                "build",
                "-t",
                ".hidden",
                str(build_ctx),
                "--image-dir",
                str(tmp_path / "cache"),
                "--json",
            ]
        )

        assert ret == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "invalid_input"

    @patch("smolvm.images.builder.DockerRootfsBuilder._build_rootfs")
    @patch("smolvm.images.builder.ensure_base_kernel_for_backend")
    @patch("smolvm.images.builder.ImageBuilder.check_docker", return_value=True)
    def test_snippet_paths_are_repr_escaped(
        self,
        _mock_docker: MagicMock,
        mock_kernel: MagicMock,
        mock_build: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The printed Python snippet must stay valid for paths containing
        quotes (regression)."""
        ctx = tmp_path / 'ctx "quoted"'
        ctx.mkdir()
        (ctx / "Dockerfile").write_text("FROM scratch\n")
        kernel = tmp_path / "vmlinux.image"
        kernel.write_bytes(b"k")
        mock_kernel.return_value = kernel
        mock_build.side_effect = _fake_build_rootfs

        ret = main(
            [
                "image",
                "build",
                "-t",
                "myimg",
                str(ctx),
                "--backend",
                "qemu",
                "--arch",
                "amd64",
                "--image-dir",
                str(tmp_path / "cache"),
            ]
        )

        assert ret == 0
        out = capsys.readouterr().out
        assert "rootfs_path='" in out.replace("\n", "")
