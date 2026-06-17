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

import hashlib
import io
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import ImageError
from smolvm.images import builder as builder_mod
from smolvm.images.builder import ImageBuilder
from smolvm.images.published import IMAGES_RELEASE_TAG

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _assert_clock_sync_loop_before_sshd(script: str) -> None:
    assert 'HWCLOCK_PATH=$(command -v "$cand" 2>/dev/null)' in script
    assert 'HWCLOCK="$HWCLOCK_PATH"' in script
    assert '"$HWCLOCK" -s -u' in script
    assert script.index('"$HWCLOCK" -s -u') < script.index("/usr/sbin/sshd")


def _assert_guest_agent_starts_before_network_and_ssh(script: str) -> None:
    agent_start = script.index("/usr/local/bin/smolvm-guest-agent --listen vsock://1024")
    network_ready = (
        script.index('log_ts "net-ready"')
        if 'log_ts "net-ready"' in script
        else script.index("hostname smolvm")
    )
    assert agent_start < network_ready
    assert agent_start < script.index("ssh-keygen -t ed25519")
    assert agent_start < script.index("/usr/sbin/sshd")


def _assert_startup_timestamp_markers(script: str) -> None:
    if script.index('log_ts "clock-sync-start"') > script.index('log_ts "ssh-authkey-inject-done"'):
        ordered_stages = [
            "init-start",
            "mounts-ready",
            "root-ready",
            "guest-agent-start",
            "guest-agent-started",
            "net-config-start",
            "net-ready",
            "ssh-hostkey-check-start",
            "ssh-hostkey-check-done",
            "ssh-authkey-inject-start",
            "ssh-authkey-inject-done",
            "clock-sync-start",
            "sshd-start",
            "sshd-invoked",
            "init-complete",
        ]
    else:
        ordered_stages = [
            "init-start",
            "mounts-ready",
            "root-ready",
            "guest-agent-start",
            "guest-agent-started",
            "net-config-start",
            "net-ready",
            "clock-sync-start",
            "ssh-hostkey-check-start",
            "ssh-hostkey-check-done",
            "ssh-authkey-inject-start",
            "ssh-authkey-inject-done",
            "sshd-start",
            "sshd-invoked",
            "init-complete",
        ]

    positions = []
    for stage in ordered_stages:
        positions.append(script.index(f'log_ts "{stage}"'))
        if stage == "clock-sync-start":
            clock_sync_done_positions = [
                script.index(f'log_ts "{candidate}"')
                for candidate in ("clock-sync-started", "clock-sync-disabled")
                if f'log_ts "{candidate}"' in script
            ]
            assert clock_sync_done_positions
            positions.extend(sorted(clock_sync_done_positions))
    assert positions == sorted(positions)


def test_ci_preset_init_launches_guest_agent_before_sshd() -> None:
    """The CI publish pipeline's /init must launch the agent before sshd,
    mirroring the Python builder. These two init paths have to stay in sync —
    PR #310 baked the agent only into the Python builder, which is why
    published images shipped without it until this fix."""
    script = (_REPO_ROOT / "scripts" / "ci" / "preset-init.sh").read_text()
    assert "/usr/local/bin/smolvm-guest-agent --listen vsock://1024" in script
    assert "python3 /usr/local/bin/smolvm-guest-agent" not in script
    assert "ssh-keygen -A" not in script
    assert "ssh-keygen -t ed25519" in script
    _assert_guest_agent_starts_before_network_and_ssh(script)
    _assert_startup_timestamp_markers(script)


def test_ci_build_preset_bakes_guest_agent() -> None:
    """build-preset.sh must copy the guest agent into every published rootfs."""
    script = (_REPO_ROOT / "scripts" / "ci" / "build-preset.sh").read_text()
    assert "target/$GUEST_AGENT_TARGET/release/smolvm-guest-agent" in script
    assert "src/smolvm/guest_agent/agent.py" not in script
    assert "/usr/local/bin/smolvm-guest-agent" in script


def test_published_image_workflow_uploads_guest_agent_binaries() -> None:
    """The image release workflow should publish standalone guest-agent binaries."""
    workflow = (_REPO_ROOT / ".github" / "workflows" / "build-published-images.yml").read_text()
    assert "guest-agent-binaries:" in workflow
    assert "if: ${{ inputs.presets == 'all' }}" in workflow
    assert 'cargo build --release --target "$target" -p smolvm-guest-agent' in workflow
    assert "smolvm-guest-agent-linux-amd64" in workflow
    assert "smolvm-guest-agent-linux-arm64" in workflow
    assert '"$ASSET_NAME"' in workflow
    assert '"${ASSET_NAME}.sha256"' in workflow


def test_published_image_workflow_openclaw_uses_authenticated_kernel_download() -> None:
    """OpenClaw builds must not fetch draft kernel assets via public URLs."""
    workflow = (_REPO_ROOT / ".github" / "workflows" / "build-published-images.yml").read_text()
    assert "Download OpenClaw kernel" in workflow
    assert "vmlinux-${ARCH}.elf" in workflow
    assert "KERNEL_PATH: ${{ steps.openclaw_kernel.outputs.path }}" in workflow
    assert "kernel_url=kernel_url" in workflow


def test_e2e_uses_image_release_fallback_until_pinned_release_is_public() -> None:
    workflow = (_REPO_ROOT / ".github" / "workflows" / "e2e.yml").read_text()
    assert "Resolve image release fallback" in workflow
    assert "draft == false and .prerelease == false" in workflow
    assert "application/vnd.github.raw" in workflow
    assert "contents/src/smolvm/images/published.py?ref=$fallback" in workflow
    assert "using published-image catalog from $fallback" in workflow
    assert "SMOLVM_IMAGES_RELEASE_TAG=${SMOLVM_IMAGES_RELEASE_TAG:-}" in workflow
    assert "github.event_name == 'pull_request'" not in workflow


def test_smoke_published_images_uses_pinned_image_release_tag() -> None:
    """The smoke workflow should read the same image tag as build workflows."""
    workflow = (_REPO_ROOT / ".github" / "workflows" / "smoke-published-images.yml").read_text()
    assert IMAGES_RELEASE_TAG in workflow
    assert "IMAGES_RELEASE_TAG" in workflow
    assert "pyproject.toml" not in workflow
    assert "images-v${version}" not in workflow


def test_smoke_published_images_waits_for_rootfs_publish() -> None:
    workflow = (_REPO_ROOT / ".github" / "workflows" / "smoke-published-images.yml").read_text()
    assert 'workflows: ["Build Published Images"]' in workflow
    assert "Build microvm Kernel" not in workflow
    assert "contents: write" in workflow


def test_guest_agent_source_digest_tracks_rust_crate() -> None:
    digest = builder_mod._guest_agent_source_digest()
    assert len(digest) == 64
    assert (builder_mod._GUEST_AGENT_CRATE_DIR / "src" / "main.rs").is_file()


def test_guest_agent_source_digest_tracks_release_binary_without_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SMOLVM_GUEST_AGENT_BINARY", raising=False)
    monkeypatch.setattr(builder_mod, "_has_guest_agent_source_checkout", lambda: False)
    monkeypatch.setattr(
        builder_mod,
        "_guest_agent_release_asset",
        lambda: ("https://example.invalid/agent-a", "smolvm-guest-agent-linux-amd64", "a" * 64),
    )
    first = builder_mod._guest_agent_source_digest()
    monkeypatch.setattr(
        builder_mod,
        "_guest_agent_release_asset",
        lambda: ("https://example.invalid/agent-b", "smolvm-guest-agent-linux-amd64", "b" * 64),
    )
    second = builder_mod._guest_agent_source_digest()

    assert len(first) == 64
    assert len(second) == 64
    assert first != second


def test_guest_agent_source_digest_tracks_env_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "custom-agent"
    binary.write_bytes(b"first")
    monkeypatch.setenv("SMOLVM_GUEST_AGENT_BINARY", str(binary))

    first = builder_mod._guest_agent_source_digest()
    binary.write_bytes(b"second")
    second = builder_mod._guest_agent_source_digest()

    assert len(first) == 64
    assert len(second) == 64
    assert first != second


def test_guest_agent_binary_honors_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "custom-agent"
    binary.write_bytes(b"custom")
    monkeypatch.setenv("SMOLVM_GUEST_AGENT_BINARY", str(binary))
    monkeypatch.setattr(
        builder_mod,
        "_download_guest_agent_binary",
        lambda: pytest.fail("env override should not download"),
    )

    assert builder_mod._guest_agent_binary() == binary


def test_guest_agent_binary_downloads_release_without_source_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"release-agent"
    expected_sha = hashlib.sha256(payload).hexdigest()
    opened_urls: list[str] = []

    class Response(io.BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            self.close()

    def fake_urlopen(url: str, **_kwargs: object) -> Response:
        opened_urls.append(url)
        return Response(payload)

    monkeypatch.delenv("SMOLVM_GUEST_AGENT_BINARY", raising=False)
    monkeypatch.setattr(builder_mod, "_has_guest_agent_source_checkout", lambda: False)
    monkeypatch.setattr(builder_mod, "_guest_agent_binary_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(builder_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        builder_mod,
        "_guest_agent_release_asset",
        lambda: ("https://example.invalid/agent", "smolvm-guest-agent-linux-amd64", expected_sha),
    )
    monkeypatch.setattr(builder_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        builder_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("installed package should not run cargo"),
    )

    binary = builder_mod._guest_agent_binary()

    assert binary.name == "smolvm-guest-agent-linux-amd64"
    assert binary.read_bytes() == payload
    assert binary.stat().st_mode & 0o111
    assert opened_urls == ["https://example.invalid/agent"]
    assert builder_mod._guest_agent_binary() == binary
    assert opened_urls == ["https://example.invalid/agent"]


def test_guest_agent_binary_rejects_bad_release_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response(io.BytesIO):
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_exc: object) -> None:
            self.close()

    monkeypatch.delenv("SMOLVM_GUEST_AGENT_BINARY", raising=False)
    monkeypatch.setattr(builder_mod, "_guest_agent_binary_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(builder_mod.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        builder_mod,
        "_guest_agent_release_asset",
        lambda: ("https://example.invalid/agent", "smolvm-guest-agent-linux-amd64", "0" * 64),
    )
    monkeypatch.setattr(
        builder_mod.urllib.request,
        "urlopen",
        lambda _url, **_kwargs: Response(b"not-the-pinned-binary"),
    )

    with pytest.raises(ImageError, match="SHA-256 verification"):
        builder_mod._download_guest_agent_binary()


def test_base_init_script_launches_guest_agent_before_sshd() -> None:
    script = ImageBuilder()._default_init_script()
    assert "/usr/local/bin/smolvm-guest-agent --listen vsock://1024" in script
    assert "python3 /usr/local/bin/smolvm-guest-agent" not in script
    assert "ssh-keygen -A" not in script
    assert "ssh-keygen -t ed25519" in script
    # The agent must start before sshd so the channel is up independent of it.
    _assert_guest_agent_starts_before_network_and_ssh(script)
    _assert_startup_timestamp_markers(script)


def test_base_init_script_runs_clock_sync_loop() -> None:
    """The PID 1 init must keep the guest clock pinned to the host RTC so it
    recovers from host-sleep drift (issue #330)."""
    script = ImageBuilder()._default_init_script()
    _assert_clock_sync_loop_before_sshd(script)
    assert script.index('echo "SmolVM init: clock-sync loop started') < script.index(
        'log_ts "clock-sync-started"'
    )
    assert script.index('echo "SmolVM init: hwclock not found') < script.index(
        'log_ts "clock-sync-disabled"'
    )


def test_ci_preset_init_runs_clock_sync_loop() -> None:
    """The CI publish pipeline's /init must carry the same clock-sync loop as
    the Python builder — the two init paths have to stay in sync."""
    script = (_REPO_ROOT / "scripts" / "ci" / "preset-init.sh").read_text()
    _assert_clock_sync_loop_before_sshd(script)


def test_fingerprint_tracks_guest_agent(tmp_path: Path) -> None:
    builder = ImageBuilder(cache_dir=tmp_path)
    fp = builder._fingerprint_with_content({"x": 1}, "FROM alpine", "init")
    assert "_guest_agent_source_sha256" in fp


@pytest.mark.parametrize(
    ("method_name", "expected_base"),
    [("build_alpine_ssh", "alpine"), ("build_debian_ssh_key", "debian")],
)
def test_base_images_are_not_responsible_for_agent_runtime(
    method_name: str, expected_base: str, tmp_path: Path
) -> None:
    """The Rust agent is a standalone binary, so python3 is not required for it."""
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
    assert "smolvm-guest-agent" not in captured["dockerfile"]


@patch("smolvm.images.builder.subprocess.run")
@patch("smolvm.images.builder.run_command")
def test_do_build_bakes_agent_into_context(
    mock_run_command: MagicMock, mock_subprocess_run: MagicMock, tmp_path: Path
) -> None:
    """_do_build must drop the agent file into the build context and COPY it."""
    builder = ImageBuilder(cache_dir=tmp_path / "images")
    fake_agent = tmp_path / "smolvm-guest-agent"
    fake_agent.write_bytes(b"rust-agent")
    fake_agent.chmod(0o755)
    captured: dict[str, object] = {}

    def _subprocess_side_effect(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["docker", "build"]:
            context = Path(cmd[-1])
            captured["dockerfile"] = (context / "Dockerfile").read_text()
            agent_file = context / builder_mod._GUEST_AGENT_BUILD_FILE
            captured["agent_present"] = agent_file.exists()
            captured["agent_bytes"] = agent_file.read_bytes() if agent_file.exists() else b""
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
        patch("smolvm.images.builder._guest_agent_binary", return_value=fake_agent),
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
    assert captured["agent_bytes"] == b"rust-agent"
