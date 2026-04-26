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

from smolvm.exceptions import ImageError, SmolVMError
from smolvm.images.builder import ImageBuilder
from smolvm.images.container_runtime import (
    ContainerRuntime,
    _ProbeResult,
    _ProbeStatus,
    container_runtime_error,
    detect_container_runtime,
    runtime_health,
)
from smolvm.runtime.boot_profiles import KernelBootProfile, resolve_kernel_url


def _builder_with_runtime(
    cache_dir: Path,
    *,
    kind: str = "podman",
    binary: str = "/usr/bin/podman",
) -> ImageBuilder:
    """Build an ImageBuilder with a fixed runtime, skipping probing."""
    builder = ImageBuilder(cache_dir=cache_dir)
    builder._runtime = ContainerRuntime(
        kind=kind,  # type: ignore[arg-type]
        binary=Path(binary),
        needs_machine=False,
    )
    return builder


def _ok_subprocess_run(
    cmd: list[str], *args: object, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    if len(cmd) >= 2 and cmd[1] == "create":
        return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class TestContainerRuntimeDiagnostics:
    """Diagnostics for missing or unreachable container runtimes."""

    def test_error_when_no_runtime_installed(self) -> None:
        probe = _ProbeResult(kind="podman", binary=None, status=_ProbeStatus.NO_BINARY)
        error = container_runtime_error(probe)
        message = str(error)
        assert "SmolVM needs Podman to build images" in message
        # Recovery hint must name the install command.
        assert "podman" in message.lower()

    def test_error_when_docker_daemon_down(self) -> None:
        probe = _ProbeResult(
            kind="docker",
            binary=Path("/usr/bin/docker"),
            status=_ProbeStatus.DAEMON_DOWN,
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        )
        error = container_runtime_error(probe)
        message = str(error)
        assert "could not reach the Docker daemon" in message
        assert "Start Docker Desktop or the Docker service" in message
        assert "Cannot connect to the Docker daemon" in message

    def test_error_when_docker_socket_permission_denied(self) -> None:
        probe = _ProbeResult(
            kind="docker",
            binary=Path("/usr/bin/docker"),
            status=_ProbeStatus.PERMISSION_DENIED,
            stderr="permission denied while connecting to docker.sock",
        )
        error = container_runtime_error(probe)
        message = str(error)
        assert "cannot reach the Docker socket" in message
        # Recovery should point to rootless Podman as the preferred fix.
        assert "rootless" in message.lower()
        assert "podman" in message.lower()

    def test_error_when_podman_machine_not_running(self) -> None:
        probe = _ProbeResult(
            kind="podman",
            binary=Path("/opt/homebrew/bin/podman"),
            status=_ProbeStatus.MACHINE_NOT_RUNNING,
            stderr="Cannot connect to Podman. Run 'podman machine start'.",
        )
        error = container_runtime_error(probe)
        message = str(error)
        assert "Linux VM is not running" in message
        assert "podman machine start" in message

    def test_error_under_override_names_forced_runtime(self) -> None:
        """SMOLVM_CONTAINER_RUNTIME=docker + Docker missing should not tell
        the user to install Podman — that won't fix the build."""
        probe = _ProbeResult(kind="docker", binary=None, status=_ProbeStatus.NO_BINARY)
        with patch.dict("os.environ", {"SMOLVM_CONTAINER_RUNTIME": "docker"}):
            error = container_runtime_error(probe)
        message = str(error)
        assert "SMOLVM_CONTAINER_RUNTIME=docker" in message
        assert "Install Docker" in message
        assert "unset SMOLVM_CONTAINER_RUNTIME" in message
        # Must not steer the user to install Podman in this case.
        assert "needs Podman" not in message


class TestRuntimeHealthForDoctor:
    """`runtime_health()` powers the doctor check — must distinguish
    'no runtime' from 'runtime installed but unhealthy'."""

    def test_machine_not_running_surfaces_specific_fix(self) -> None:
        with (
            patch(
                "smolvm.images.container_runtime.shutil.which",
                side_effect=lambda name: "/opt/homebrew/bin/podman" if name == "podman" else None,
            ),
            patch(
                "smolvm.images.container_runtime.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    1,
                    ["podman", "info"],
                    stderr="Cannot connect to Podman. Run 'podman machine start' first.",
                ),
            ),
        ):
            health = runtime_health()
        assert health.status == "warn"
        assert "Linux VM is not running" in health.detail
        assert health.fix is not None
        assert "podman machine start" in health.fix
        # The user has Podman — must not tell them to install it again.
        assert "brew install" not in (health.fix or "")

    def test_docker_daemon_down_surfaces_specific_fix(self) -> None:
        with (
            patch(
                "smolvm.images.container_runtime.shutil.which",
                side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None,
            ),
            patch(
                "smolvm.images.container_runtime.subprocess.run",
                side_effect=subprocess.CalledProcessError(
                    1,
                    ["docker", "info"],
                    stderr=(
                        "Cannot connect to the Docker daemon at "
                        "unix:///var/run/docker.sock. Is the docker daemon running?"
                    ),
                ),
            ),
        ):
            health = runtime_health()
        assert health.status == "warn"
        assert "daemon is not running" in health.detail
        assert health.fix is not None
        assert "Docker Desktop" in health.fix or "Docker service" in health.fix

    def test_no_runtime_returns_install_fix(self) -> None:
        with patch("smolvm.images.container_runtime.shutil.which", return_value=None):
            health = runtime_health()
        assert health.status == "warn"
        assert "No container runtime found" in health.detail
        assert health.fix is not None
        assert "podman" in health.fix.lower()

    def test_ok_runtime_passes(self) -> None:
        with (
            patch(
                "smolvm.images.container_runtime.shutil.which",
                side_effect=lambda name: f"/usr/bin/{name}" if name == "podman" else None,
            ),
            patch(
                "smolvm.images.container_runtime.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ),
        ):
            health = runtime_health()
        assert health.status == "pass"
        assert "podman" in health.detail
        assert health.fix is None


class TestContainerRuntimeDetection:
    """Detection ordering and env override behaviour."""

    def test_prefers_podman_when_both_present(self) -> None:
        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in {"podman", "docker"} else None

        with (
            patch("smolvm.images.container_runtime.shutil.which", side_effect=fake_which),
            patch(
                "smolvm.images.container_runtime.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ),
            patch.dict("os.environ", {}, clear=False),
        ):
            runtime = detect_container_runtime()

        assert runtime is not None
        assert runtime.kind == "podman"

    def test_falls_back_to_docker_when_podman_missing(self) -> None:
        def fake_which(name: str) -> str | None:
            return "/usr/bin/docker" if name == "docker" else None

        with (
            patch("smolvm.images.container_runtime.shutil.which", side_effect=fake_which),
            patch(
                "smolvm.images.container_runtime.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ),
        ):
            runtime = detect_container_runtime()

        assert runtime is not None
        assert runtime.kind == "docker"

    def test_honors_env_override(self) -> None:
        def fake_which(name: str) -> str | None:
            return f"/usr/bin/{name}" if name in {"podman", "docker"} else None

        with (
            patch("smolvm.images.container_runtime.shutil.which", side_effect=fake_which),
            patch(
                "smolvm.images.container_runtime.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            ),
            patch.dict("os.environ", {"SMOLVM_CONTAINER_RUNTIME": "docker"}),
        ):
            runtime = detect_container_runtime()

        assert runtime is not None
        assert runtime.kind == "docker"

    def test_returns_none_when_neither_present(self) -> None:
        with patch("smolvm.images.container_runtime.shutil.which", return_value=None):
            runtime = detect_container_runtime()

        assert runtime is None


class TestImageBuilderLoopFs:
    """Tests for image builder loopfs helper integration."""

    def test_run_loopfs_missing_helper_raises(self, tmp_path: Path) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        with (
            patch.object(ImageBuilder, "_loopfs_helper_path", return_value=None),
            pytest.raises(ImageError, match="smolvm setup"),
        ):
            builder._run_loopfs("mount", Path("/tmp/rootfs.ext4"), Path("/tmp/mnt"))

    @patch("smolvm.images.builder.run_command")
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
            pytest.raises(ImageError, match="smolvm setup"),
        ):
            builder._run_loopfs("mount", Path("/tmp/rootfs.ext4"), Path("/tmp/mnt"))

    @patch("smolvm.images.builder.subprocess.run")
    @patch("smolvm.images.builder.run_command")
    def test_do_build_uses_loopfs_helper(
        self, mock_run_command: MagicMock, mock_subprocess_run: MagicMock, tmp_path: Path
    ) -> None:
        builder = _builder_with_runtime(tmp_path / "images")
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

    @patch("smolvm.images.builder.subprocess.run")
    @patch("smolvm.images.builder.run_command")
    def test_do_build_uses_container_fallback_when_loopfs_missing(
        self, mock_run_command: MagicMock, mock_subprocess_run: MagicMock, tmp_path: Path
    ) -> None:
        builder = _builder_with_runtime(tmp_path / "images", binary="/usr/bin/podman")

        def _subprocess_side_effect(
            cmd: list[str], *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if len(cmd) >= 2 and cmd[1] == "create":
                return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")

            if len(cmd) >= 2 and cmd[1] == "export":
                tar_index = cmd.index("-o") + 1
                tar_path = Path(cmd[tar_index])
                with tarfile.open(tar_path, "w"):
                    pass
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            if len(cmd) >= 2 and cmd[1] == "run":
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
        run_calls = [
            call
            for call in mock_subprocess_run.call_args_list
            if len(call.args[0]) >= 2 and call.args[0][1] == "run"
        ]
        assert len(run_calls) == 1
        # The configured runtime binary is used, not a hardcoded "docker".
        assert run_calls[0].args[0][0] == "/usr/bin/podman"
        assert rootfs_path.exists()

    @patch("smolvm.images.builder.subprocess.run")
    @patch("smolvm.images.builder.run_command")
    def test_do_build_preserves_tar_error_when_unmount_fails(
        self, mock_run_command: MagicMock, mock_subprocess_run: MagicMock, tmp_path: Path
    ) -> None:
        builder = _builder_with_runtime(tmp_path / "images")

        def _subprocess_side_effect(
            cmd: list[str], *args: object, **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            if len(cmd) >= 2 and cmd[1] == "create":
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


@pytest.mark.parametrize("method_name", ["build_alpine_ssh_key", "build_debian_ssh_key"])
def test_rebuild_preserves_cached_artifacts_when_runtime_is_unavailable(
    method_name: str,
    tmp_path: Path,
) -> None:
    """Rebuild paths should not evict cached files before the runtime is confirmed."""
    builder = ImageBuilder(cache_dir=tmp_path / "images")
    image_name = "cached-image"
    image_dir = builder.cache_dir / image_name
    image_dir.mkdir(parents=True)
    kernel_path = image_dir / "vmlinux.bin"
    rootfs_path = image_dir / "rootfs.ext4"
    kernel_path.write_bytes(b"kernel")
    rootfs_path.write_bytes(b"rootfs")

    build_method = getattr(builder, method_name)

    with (
        patch.object(
            ImageBuilder,
            "_resolve_public_key",
            return_value="ssh-ed25519 AAAA user@test",
        ),
        patch.object(
            ImageBuilder,
            "_resolve_kernel_url",
            return_value="https://example.invalid/vmlinux",
        ),
        patch.object(ImageBuilder, "_check_fingerprint", return_value=False),
        patch.object(ImageBuilder, "check_docker", return_value=False),
        patch.object(
            ImageBuilder,
            "docker_requirement_error",
            return_value=ImageError("runtime unavailable"),
        ),
        pytest.raises(ImageError, match="runtime unavailable"),
    ):
        build_method("ignored", name=image_name)

    assert kernel_path.exists()
    assert rootfs_path.exists()
