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

"""Tests that the guest agent is baked into and launched by built images."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.images import builder as builder_mod
from smolvm.images.builder import ImageBuilder


def test_guest_agent_source_is_the_real_agent() -> None:
    source = builder_mod._guest_agent_source()
    assert "AF_VSOCK" in source
    assert "smolvm-guest-agent listening" in source


def test_base_init_script_launches_guest_agent_before_sshd() -> None:
    script = ImageBuilder()._default_init_script()
    assert "python3 /usr/local/bin/smolvm-guest-agent" in script
    # The agent must start before sshd so the channel is up independent of it.
    assert script.index("smolvm-guest-agent") < script.index("/usr/sbin/sshd")


def test_fingerprint_tracks_guest_agent(tmp_path: Path) -> None:
    builder = ImageBuilder(cache_dir=tmp_path)
    fp = builder._fingerprint_with_content({"x": 1}, "FROM alpine", "init")
    assert "_guest_agent_sha256" in fp


@pytest.mark.parametrize(
    ("method_name", "expected_base"),
    [("build_alpine_ssh", "alpine"), ("build_debian_ssh_key", "debian")],
)
def test_base_images_install_python3_for_agent(
    method_name: str, expected_base: str, tmp_path: Path
) -> None:
    """Alpine/Debian base images must ship python3 so the agent can run."""
    builder = ImageBuilder(cache_dir=tmp_path / "images")
    captured: dict[str, str] = {}

    def _capture(
        name: str,
        dockerfile_content: str,
        init_script: str,
        image_dir: Path,
        kernel_path: Path,
        rootfs_path: Path,
        rootfs_size_mb: int,
        **kwargs: object,
    ) -> None:
        captured["dockerfile"] = dockerfile_content
        kernel_path.touch()
        rootfs_path.touch()

    with (
        patch.object(ImageBuilder, "check_docker", return_value=True),
        patch.object(ImageBuilder, "_resolve_public_key", return_value="ssh-ed25519 AAAA u@t"),
        patch.object(
            ImageBuilder, "_resolve_kernel_url", return_value="https://example.invalid/vmlinux"
        ),
        patch.object(ImageBuilder, "_do_build", side_effect=_capture),
    ):
        getattr(builder, method_name)("ssh-ed25519 AAAA u@t")

    assert expected_base in captured["dockerfile"]
    assert "python3" in captured["dockerfile"]


@patch("smolvm.images.builder.subprocess.run")
@patch("smolvm.images.builder.run_command")
def test_do_build_bakes_agent_into_context(
    mock_run_command: MagicMock, mock_subprocess_run: MagicMock, tmp_path: Path
) -> None:
    """_do_build must drop the agent file into the build context and COPY it."""
    builder = ImageBuilder(cache_dir=tmp_path / "images")
    captured: dict[str, object] = {}

    def _subprocess_side_effect(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["docker", "build"]:
            context = Path(cmd[-1])
            captured["dockerfile"] = (context / "Dockerfile").read_text()
            agent_file = context / builder_mod._GUEST_AGENT_BUILD_FILE
            captured["agent_present"] = agent_file.exists()
            captured["agent_text"] = agent_file.read_text() if agent_file.exists() else ""
        if cmd[:2] == ["docker", "create"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mock_subprocess_run.side_effect = _subprocess_side_effect

    image_dir = tmp_path / "image"
    image_dir.mkdir()

    with (
        patch.object(
            ImageBuilder,
            "_loopfs_helper_path",
            return_value=Path("/usr/local/libexec/smolvm-loopfs-helper"),
        ),
        patch.object(ImageBuilder, "_create_ext4_with_loopfs"),
        patch.object(ImageBuilder, "_download_kernel"),
    ):
        builder._do_build(
            name="demo",
            dockerfile_content="FROM scratch\n",
            init_script="#!/bin/sh\n",
            image_dir=image_dir,
            kernel_path=image_dir / "vmlinux.bin",
            rootfs_path=image_dir / "rootfs.ext4",
            rootfs_size_mb=8,
        )

    assert (
        f"COPY {builder_mod._GUEST_AGENT_BUILD_FILE} {builder_mod._GUEST_AGENT_GUEST_PATH}"
        in (captured["dockerfile"])
    )
    assert captured["agent_present"] is True
    assert "AF_VSOCK" in captured["agent_text"]
