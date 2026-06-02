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
from smolvm.images.published import BASE_KERNELS
from smolvm.runtime.boot_profiles import KernelBootProfile


def _ok_subprocess_run(
    cmd: list[str], *args: object, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    if cmd[:2] == ["docker", "create"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="container-id\n", stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_base_init_script_uses_cmdline_netmask_and_gateway_dns() -> None:
    script = ImageBuilder()._default_init_script()

    assert "netmask_to_prefix()" in script
    assert 'NETMASK=$(echo "$IP_FIELDS" | cut -d: -f4)' in script
    assert 'PREFIX=$(netmask_to_prefix "$NETMASK") || PREFIX=24' in script
    assert 'ip addr add "${GUEST_IP}/${PREFIX}" dev eth0' in script
    assert 'if [ -n "$GATEWAY" ]; then' in script
    assert 'echo "nameserver ${GATEWAY}" > /etc/resolv.conf' in script
    assert 'ip addr add "${GUEST_IP}/24"' not in script


def test_preset_init_script_uses_cmdline_netmask_and_gateway_dns() -> None:
    script = Path("scripts/ci/preset-init.sh").read_text()

    assert "netmask_to_prefix()" in script
    assert 'NETMASK=$(echo "$IP_FIELDS" | cut -d: -f4)' in script
    assert 'PREFIX=$(netmask_to_prefix "$NETMASK") || PREFIX=24' in script
    assert 'ip addr add "${GUEST_IP}/${PREFIX}" dev eth0' in script
    assert 'if [ -n "$GATEWAY" ]; then' in script
    assert 'echo "nameserver ${GATEWAY}" > /etc/resolv.conf' in script
    assert 'ip addr add "${GUEST_IP}/24"' not in script


def test_base_init_script_keeps_tmp_on_root_disk() -> None:
    script = ImageBuilder()._default_init_script()

    assert "mount -t tmpfs tmpfs /run" in script
    assert "mount -t tmpfs tmpfs /tmp" not in script
    assert "mkdir -p /run/sshd /var/log /tmp" in script
    assert "chmod 1777 /tmp" in script


def test_preset_init_script_keeps_tmp_on_root_disk() -> None:
    script = Path("scripts/ci/preset-init.sh").read_text()

    assert "mount -t tmpfs tmpfs /run" in script
    assert "mount -t tmpfs tmpfs /tmp" not in script
    assert "mkdir -p /run/sshd /var/log /tmp" in script
    assert "chmod 1777 /tmp" in script


class TestDockerDiagnostics:
    """Tests for Docker availability diagnostics."""

    def test_docker_requirement_error_when_docker_missing(self, tmp_path: Path) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        with patch("smolvm.images.builder.shutil.which", return_value=None):
            error = builder.docker_requirement_error()

        assert str(error) == (
            "Docker is required to build images. "
            "Install Docker Desktop (macOS) or docker.io (Linux)."
        )

    @patch("smolvm.images.builder.subprocess.run")
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

        with patch("smolvm.images.builder.shutil.which", return_value="/usr/bin/docker"):
            error = builder.docker_requirement_error()

        assert "could not reach the Docker daemon" in str(error)
        assert "Start Docker Desktop or the Docker service" in str(error)
        assert "Cannot connect to the Docker daemon" in str(error)

    @patch("smolvm.images.builder.subprocess.run")
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

        with patch("smolvm.images.builder.shutil.which", return_value="/usr/bin/docker"):
            error = builder.docker_requirement_error()

        assert "cannot access the Docker daemon socket" in str(error)
        assert "docker.sock" in str(error)


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


def _apk_installs_python3(dockerfile: str) -> bool:
    """Return True if python3 is an actual `apk add` package, not just text.

    Joins backslash-continued lines so a multi-line `apk add ... \\ python3`
    counts, but a bare comment mentioning python3 does not.
    """
    joined = dockerfile.replace("\\\n", " ")
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "apk add" in stripped and "python3" in stripped.split("apk add", 1)[1]:
            return True
    return False


class TestAgentRuntimeBakedIntoImages:
    """Every SSH-capable recipe must install python3.

    The vsock guest agent (baked into every image by ``_do_build``) is a
    python3 script that ``/init`` only launches ``if command -v python3``.
    A recipe that omits python3 silently disables the agent, which makes the
    host pay the full ``_VSOCK_AUTO_PROBE_TIMEOUT`` before falling back to SSH.
    This locks the runtime in so the two recipes can't drift again.
    """

    @patch.object(ImageBuilder, "_host_arch_key", return_value="x86_64")
    @patch.object(ImageBuilder, "check_docker", return_value=True)
    @patch.object(ImageBuilder, "_do_build")
    def test_build_alpine_ssh_key_installs_python3(
        self,
        mock_do_build: MagicMock,
        _mock_check_docker: MagicMock,
        _mock_host_arch_key: MagicMock,
        tmp_path: Path,
    ) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        def _fake_do_build(
            name: str,
            dockerfile_content: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            assert _apk_installs_python3(dockerfile_content), (
                "build_alpine_ssh_key must install python3 (in an apk add) so "
                "the vsock guest agent can run; without it the host pays an 8s "
                "vsock probe."
            )
            args[2].touch()  # kernel_path
            args[3].touch()  # rootfs_path

        mock_do_build.side_effect = _fake_do_build

        builder.build_alpine_ssh_key(
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey user@test"
        )

    @patch.object(ImageBuilder, "_host_arch_key", return_value="x86_64")
    @patch.object(ImageBuilder, "check_docker", return_value=True)
    @patch.object(ImageBuilder, "_do_build")
    def test_build_alpine_ssh_installs_python3(
        self,
        mock_do_build: MagicMock,
        _mock_check_docker: MagicMock,
        _mock_host_arch_key: MagicMock,
        tmp_path: Path,
    ) -> None:
        builder = ImageBuilder(cache_dir=tmp_path / "images")

        def _fake_do_build(
            name: str,
            dockerfile_content: str,
            *args: object,
            **kwargs: object,
        ) -> None:
            assert _apk_installs_python3(dockerfile_content)
            args[2].touch()
            args[3].touch()

        mock_do_build.side_effect = _fake_do_build

        builder.build_alpine_ssh()


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
            # Post-0.0.14a0 the kernel URL resolves to the SmolVM-built
            # base kernel. Builder default is the ELF format (Firecracker —
            # the typical Linux backend); QEMU callers thread an explicit
            # kernel_url override via _build_auto_config.
            assert kwargs["kernel_url"] == BASE_KERNELS["amd64"].elf_url
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

    @patch("smolvm.images.builder.subprocess.run")
    @patch("smolvm.images.builder.run_command")
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


@pytest.mark.parametrize("method_name", ["build_alpine_ssh_key", "build_debian_ssh_key"])
def test_rebuild_preserves_cached_artifacts_when_docker_is_unavailable(
    method_name: str,
    tmp_path: Path,
) -> None:
    """Rebuild paths should not evict cached files before Docker is confirmed available."""
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
            return_value=ImageError("docker unavailable"),
        ),
        pytest.raises(ImageError, match="docker unavailable"),
    ):
        build_method("ignored", name=image_name)

    assert kernel_path.exists()
    assert rootfs_path.exists()


class TestFingerprintWithContent:
    """Tests for the cache-key augmentation that includes Dockerfile/init hashes.

    Without this, edits to the Dockerfile or init script don't invalidate the
    local cache — the user keeps getting the old image even though the recipe
    changed.
    """

    def test_same_inputs_and_content_produce_stable_key(self, tmp_path: Path) -> None:
        builder = ImageBuilder(cache_dir=tmp_path)
        inputs = {"size_mb": 512, "ssh_password": "smolvm"}
        a = builder._fingerprint_with_content(inputs, "FROM alpine:3.19", "#!/bin/sh\nexec /init")
        b = builder._fingerprint_with_content(inputs, "FROM alpine:3.19", "#!/bin/sh\nexec /init")
        assert a == b

    def test_dockerfile_change_invalidates_key(self, tmp_path: Path) -> None:
        builder = ImageBuilder(cache_dir=tmp_path)
        inputs = {"size_mb": 512}
        before = builder._fingerprint_with_content(inputs, "FROM alpine:3.19", "init")
        after = builder._fingerprint_with_content(inputs, "FROM alpine:3.20", "init")
        assert before["_dockerfile_sha256"] != after["_dockerfile_sha256"]

    def test_init_script_change_invalidates_key(self, tmp_path: Path) -> None:
        builder = ImageBuilder(cache_dir=tmp_path)
        inputs = {"size_mb": 512}
        before = builder._fingerprint_with_content(inputs, "FROM alpine", "echo old")
        after = builder._fingerprint_with_content(inputs, "FROM alpine", "echo new")
        assert before["_init_script_sha256"] != after["_init_script_sha256"]

    def test_inputs_passthrough(self, tmp_path: Path) -> None:
        """Augmentation must keep the original inputs alongside the content hashes."""
        builder = ImageBuilder(cache_dir=tmp_path)
        inputs = {"size_mb": 512, "ssh_password": "smolvm", "extra_packages": ["git"]}
        result = builder._fingerprint_with_content(inputs, "df", "init")
        for key, value in inputs.items():
            assert result[key] == value

    def test_old_fingerprint_files_invalidate_after_dockerfile_change(self, tmp_path: Path) -> None:
        """End-to-end: a stored fingerprint becomes stale when the Dockerfile changes.

        This is the bug that motivated the helper — without it, a fingerprint
        written before a Dockerfile edit would still match after the edit.
        """
        builder = ImageBuilder(cache_dir=tmp_path)
        image_dir = tmp_path / "preset"
        image_dir.mkdir()

        original = builder._fingerprint_with_content(
            {"ssh_password": "smolvm"}, "FROM alpine:3.19", "init"
        )
        builder._write_fingerprint(image_dir, original)
        assert builder._check_fingerprint(image_dir, original)

        # Dockerfile changes — same inputs, but the cache key shifts.
        edited = builder._fingerprint_with_content(
            {"ssh_password": "smolvm"}, "FROM alpine:3.20", "init"
        )
        assert not builder._check_fingerprint(image_dir, edited)
