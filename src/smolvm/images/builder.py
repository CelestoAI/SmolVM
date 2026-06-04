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

"""Image building utilities for SmolVM.

Automatically builds VM images with SSH using Docker.
"""

import hashlib
import json
import logging
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import typing
import urllib.error
import urllib.request
from pathlib import Path

from smolvm.exceptions import ImageError, SmolVMError
from smolvm.runtime.boot_profiles import (
    KernelBootProfile,
    normalize_arch,
)
from smolvm.utils import RUNTIME_PRIVILEGE_SETUP_HINT, run_command

logger = logging.getLogger(__name__)

# Default boot args that include init=/init for our custom init script
SSH_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"

# Boot args for OpenClaw VMs — 8250.nr_uarts=0 disables serial UART to avoid
# vCPU exits on /dev/ttyS0 writes, which become a measurable host CPU tax at
# 200+ VMs.  No console= since we don't need serial output in production.
OPENCLAW_BOOT_ARGS = "reboot=k panic=1 pci=off init=/init 8250.nr_uarts=0"

LOOPFS_HELPER_PATH = Path("/usr/local/libexec/smolvm-loopfs-helper")

# The SmolVM guest agent (vsock control plane). It is baked into every image
# built here and launched by /init. ``_GUEST_AGENT_SOURCE_PATH`` points at the
# checked-in agent module; it ships into the build context as
# ``_GUEST_AGENT_BUILD_FILE`` and lands in the guest at ``_GUEST_AGENT_GUEST_PATH``.
_GUEST_AGENT_SOURCE_PATH = Path(__file__).resolve().parents[1] / "guest_agent" / "agent.py"
_GUEST_AGENT_BUILD_FILE = "smolvm-guest-agent"
_GUEST_AGENT_GUEST_PATH = "/usr/local/bin/smolvm-guest-agent"


def _guest_agent_source() -> str:
    """Return the guest agent source baked into every built image."""
    return _GUEST_AGENT_SOURCE_PATH.read_text()


class ImageBuilder:
    """Builds custom VM images with SSH pre-configured.

    Example usage::

        from smolvm import ImageBuilder, SmolVM, VMConfig
        from smolvm.images.builder import SSH_BOOT_ARGS

        builder = ImageBuilder()
        kernel, rootfs = builder.build_alpine_ssh()

        config = VMConfig(
            vm_id="my-vm",
            kernel_path=kernel,
            rootfs_path=rootfs,
            boot_args=SSH_BOOT_ARGS,
        )
        with SmolVM(config) as vm:
            vm.start()
            # SSH into vm.get_ip() with root / smolvm
    """

    def __init__(self, cache_dir: Path | None = None):
        """Initialize the image builder.

        Args:
            cache_dir: Directory to store built images.
                Defaults to ~/.smolvm/images/
        """
        self.cache_dir = cache_dir or (Path.home() / ".smolvm" / "images")

    def check_docker(self) -> bool:
        """Check if Docker is available and the daemon is reachable."""
        docker_bin = shutil.which("docker")
        if docker_bin is None:
            return False

        try:
            subprocess.run(
                [docker_bin, "info"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def docker_requirement_error(self) -> ImageError:
        """Create a helpful Docker availability error."""
        install_hint = "Install Docker Desktop (macOS) or docker.io (Linux)."
        start_hint = (
            "Docker is installed, but SmolVM could not reach the Docker daemon. "
            "Start Docker Desktop or the Docker service and try again."
        )
        permission_hint = (
            "Docker is installed, but this user cannot access the Docker daemon socket. "
            "Make sure Docker Desktop is running or grant access to /var/run/docker.sock."
        )

        docker_bin = shutil.which("docker")
        if docker_bin is None:
            return ImageError(f"Docker is required to build images. {install_hint}")

        try:
            subprocess.run(
                [docker_bin, "info"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            return ImageError(f"Docker is required to build images. {install_hint}")
        except subprocess.TimeoutExpired:
            return ImageError(
                f"{start_hint} Original Docker error: timed out while contacting Docker."
            )
        except subprocess.CalledProcessError as exc:
            details = "\n".join(part.strip() for part in (exc.stderr, exc.stdout) if part).strip()
            details_lower = details.lower()
            if "permission denied" in details_lower and "docker.sock" in details_lower:
                return ImageError(
                    f"{permission_hint} Original Docker error: {details or 'unknown error.'}"
                )
            if (
                "cannot connect to the docker daemon" in details_lower
                or "is the docker daemon running" in details_lower
                or "error during connect" in details_lower
            ):
                return ImageError(
                    f"{start_hint} Original Docker error: {details or 'unknown error.'}"
                )
            return ImageError(
                "Docker is required to build images, but Docker could not be used successfully. "
                f"{install_hint} Original Docker error: {details or 'unknown error.'}"
            )

        return ImageError(
            "Docker is required to build images, but an unexpected Docker availability "
            "check failed."
        )

    def build_alpine_ssh(
        self,
        name: str = "alpine-ssh",
        ssh_password: str = "smolvm",
        rootfs_size_mb: int = 512,
        kernel_url: str | None = None,
        kernel_profile: KernelBootProfile = KernelBootProfile.MICROVM_DIRECT,
    ) -> tuple[Path, Path]:
        """Build Alpine Linux image with SSH server.

        Uses Docker to create a minimal Alpine Linux rootfs with:
        - OpenSSH server configured and auto-starting
        - Root password authentication
        - Custom /init script that sets up networking and starts sshd
        - DNS resolution configured

        The resulting VM must be booted with ``boot_args`` containing
        ``init=/init`` so the custom init script runs. Use the
        ``SSH_BOOT_ARGS`` constant for convenience.

        Args:
            name: Image name for caching.
            ssh_password: Root password for SSH (default: smolvm).
            rootfs_size_mb: Size of rootfs in MB (default: 512).
            kernel_url: Optional kernel URL override.

        Returns:
            Tuple of (kernel_path, rootfs_path).

        Raises:
            ImageError: If Docker is not available or build fails.
        """
        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        resolved_kernel_url = self._resolve_kernel_url(kernel_profile, kernel_url)

        # The /init script runs as PID 1 inside the VM and brings up SSH.
        init_script = self._default_init_script()

        dockerfile_content = """
FROM alpine:3.19

ARG SSH_PASSWORD

# Install SSH and networking utilities. python3 powers the SmolVM guest
# agent (vsock control plane); the agent is stdlib-only, so no pip deps.
RUN apk add --no-cache \\
    openssh \\
    iproute2 \\
    curl \\
    bash \\
    python3

# Configure SSH. Host keys are generated at first boot in /init, not here,
# so each VM gets a unique SSH identity — required for safely sharing images.
# The 'rm -f' purges any keys planted by the openssh package install (e.g.
# Debian's openssh-server postinst runs ssh-keygen -A automatically).
RUN rm -f /etc/ssh/ssh_host_* && \\
    sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && \\
    echo "root:${SSH_PASSWORD}" | chpasswd

# Install our custom init script
COPY init /init
RUN chmod +x /init
"""

        fingerprint_data = self._fingerprint_with_content(
            {
                "rootfs_size_mb": rootfs_size_mb,
                "kernel_url": resolved_kernel_url,
                "kernel_profile": kernel_profile.value,
                "ssh_password": ssh_password,
            },
            dockerfile_content,
            init_script,
        )
        if (
            kernel_path.exists()
            and rootfs_path.exists()
            and self._check_fingerprint(image_dir, fingerprint_data)
        ):
            logger.info("Image '%s' already exists and fingerprint matches at %s", name, image_dir)
            return (kernel_path, rootfs_path)

        if not self.check_docker():
            raise self.docker_requirement_error()

        logger.info("Building Alpine SSH image '%s'...", name)
        image_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._do_build(
                name,
                dockerfile_content,
                init_script,
                image_dir,
                kernel_path,
                rootfs_path,
                rootfs_size_mb,
                build_args={"SSH_PASSWORD": ssh_password},
                kernel_url=resolved_kernel_url,
                fingerprint_data=fingerprint_data,
            )
        except (subprocess.CalledProcessError, ImageError) as e:
            # Clean up partial build
            if rootfs_path.exists():
                rootfs_path.unlink()
            if kernel_path.exists():
                kernel_path.unlink()
            if isinstance(e, ImageError):
                raise
            raise ImageError(f"Image build failed: {e}") from e

        logger.info("Image '%s' built successfully at %s", name, image_dir)
        return (kernel_path, rootfs_path)

    def build_alpine_ssh_key(
        self,
        ssh_public_key: str | Path,
        name: str = "alpine-ssh-key",
        rootfs_size_mb: int = 512,
        kernel_url: str | None = None,
        kernel_profile: KernelBootProfile = KernelBootProfile.MICROVM_DIRECT,
    ) -> tuple[Path, Path]:
        """Build Alpine Linux image with key-only SSH access.

        Args:
            ssh_public_key: Public key content or path to a public key file.
            name: Image name for caching.
            rootfs_size_mb: Size of rootfs in MB.
            kernel_url: Optional kernel URL override.

        Returns:
            Tuple of (kernel_path, rootfs_path).
        """
        key_value = self._resolve_public_key(ssh_public_key)

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        resolved_kernel_url = self._resolve_kernel_url(kernel_profile, kernel_url)

        init_script = self._default_init_script()

        dockerfile_content = """
FROM alpine:3.19

# python3 powers the SmolVM guest agent (vsock control plane); the agent is
# stdlib-only, so no pip deps. Without it /init silently skips the agent and the
# host falls back to SSH after an 8s vsock probe (see _VSOCK_AUTO_PROBE_TIMEOUT).
RUN apk add --no-cache \
    openssh \
    iproute2 \
    curl \
    bash \
    python3

# Host keys generated at first boot in /init so each VM has unique identity.
# 'rm -f' purges keys planted by the openssh install postinst.
RUN rm -f /etc/ssh/ssh_host_* && \
    mkdir -p /root/.ssh && chmod 700 /root/.ssh && \
    sed -i 's/#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && \
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

COPY init /init
RUN chmod +x /init
"""

        # ssh_public_key is intentionally not in the fingerprint and not baked
        # into the rootfs. Per-VM keys are injected via the kernel cmdline at
        # boot (see VMConfig.ssh_public_key + /init parser), so two users with
        # different keys can share one cached image.
        _ = key_value
        fingerprint_data = self._fingerprint_with_content(
            {
                "rootfs_size_mb": rootfs_size_mb,
                "kernel_url": resolved_kernel_url,
                "kernel_profile": kernel_profile.value,
            },
            dockerfile_content,
            init_script,
        )

        if kernel_path.exists() and rootfs_path.exists():
            if self._check_fingerprint(image_dir, fingerprint_data):
                logger.info(
                    "Image '%s' already exists and fingerprint matches at %s", name, image_dir
                )
                return (kernel_path, rootfs_path)

            if not self.check_docker():
                raise self.docker_requirement_error()

            logger.info("Inputs changed for image '%s'. Rebuilding...", name)
            # Remove stale files
            kernel_path.unlink(missing_ok=True)
            rootfs_path.unlink(missing_ok=True)
        elif not self.check_docker():
            raise self.docker_requirement_error()

        logger.info("Building Alpine key-only SSH image '%s'...", name)
        image_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._do_build(
                name,
                dockerfile_content,
                init_script,
                image_dir,
                kernel_path,
                rootfs_path,
                rootfs_size_mb,
                kernel_url=resolved_kernel_url,
                fingerprint_data=fingerprint_data,
            )
        except (subprocess.CalledProcessError, ImageError) as e:
            if rootfs_path.exists():
                rootfs_path.unlink()
            if kernel_path.exists():
                kernel_path.unlink()
            if isinstance(e, ImageError):
                raise
            raise ImageError(f"Image build failed: {e}") from e

        logger.info("Image '%s' built successfully at %s", name, image_dir)
        return (kernel_path, rootfs_path)

    def build_debian_ssh_key(
        self,
        ssh_public_key: str | Path,
        name: str = "debian-ssh-key",
        rootfs_size_mb: int = 2048,
        base_image: str = "debian:bookworm-slim",
        kernel_url: str | None = None,
        kernel_profile: KernelBootProfile = KernelBootProfile.MICROVM_DIRECT,
    ) -> tuple[Path, Path]:
        """Build Debian Linux image with key-only SSH access.

        Args:
            ssh_public_key: Public key content or path to a public key file.
            name: Image name for caching.
            rootfs_size_mb: Size of rootfs in MB.
            base_image: Docker base image to build from.
            kernel_url: Optional kernel URL override.

        Returns:
            Tuple of (kernel_path, rootfs_path).
        """
        key_value = self._resolve_public_key(ssh_public_key)

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        resolved_kernel_url = self._resolve_kernel_url(kernel_profile, kernel_url)

        init_script = self._default_init_script()

        dockerfile_content = f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server \\
    iproute2 \\
    curl \\
    bash \\
    ca-certificates \\
    python3 \\
    && rm -rf /var/lib/apt/lists/*

# Host keys generated at first boot in /init so each VM has unique identity.
# 'rm -f' purges keys planted by the openssh-server postinst on Debian.
RUN rm -f /etc/ssh/ssh_host_* && \\
    mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh && \\
    sed -ri 's/^#?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

COPY init /init
RUN chmod +x /init
"""

        # ssh_public_key is intentionally not in the fingerprint and not baked
        # into the rootfs — see VMConfig.ssh_public_key + /init parser.
        _ = key_value
        fingerprint_data = self._fingerprint_with_content(
            {
                "rootfs_size_mb": rootfs_size_mb,
                "kernel_url": resolved_kernel_url,
                "kernel_profile": kernel_profile.value,
                "base_image": base_image,
            },
            dockerfile_content,
            init_script,
        )

        if kernel_path.exists() and rootfs_path.exists():
            if self._check_fingerprint(image_dir, fingerprint_data):
                logger.info(
                    "Image '%s' already exists and fingerprint matches at %s", name, image_dir
                )
                return (kernel_path, rootfs_path)

            if not self.check_docker():
                raise self.docker_requirement_error()

            logger.info("Inputs changed for image '%s'. Rebuilding...", name)
            # Remove stale files
            kernel_path.unlink(missing_ok=True)
            rootfs_path.unlink(missing_ok=True)
        elif not self.check_docker():
            raise self.docker_requirement_error()

        logger.info("Building Debian key-only SSH image '%s'...", name)
        image_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._do_build(
                name,
                dockerfile_content,
                init_script,
                image_dir,
                kernel_path,
                rootfs_path,
                rootfs_size_mb,
                kernel_url=resolved_kernel_url,
                fingerprint_data=fingerprint_data,
            )
        except (subprocess.CalledProcessError, ImageError) as e:
            if rootfs_path.exists():
                rootfs_path.unlink()
            if kernel_path.exists():
                kernel_path.unlink()
            if isinstance(e, ImageError):
                raise
            raise ImageError(f"Image build failed: {e}") from e

        logger.info("Image '%s' built successfully at %s", name, image_dir)
        return (kernel_path, rootfs_path)

    def build_browser_rootfs(
        self,
        ssh_public_key: str | Path,
        name: str = "browser-chromium",
        rootfs_size_mb: int = 4096,
        base_image: str = "debian:bookworm-slim",
        kernel_url: str | None = None,
        kernel_profile: KernelBootProfile = KernelBootProfile.MICROVM_DIRECT,
    ) -> tuple[Path, Path]:
        """Build a Chromium browser image with optional live-view tooling.

        The resulting image includes:
        - Chromium with remote debugging enabled at runtime
        - OpenSSH server for orchestration and artifact collection
        - Xvfb + Openbox + x11vnc + noVNC/websockify for live mode
        - ffmpeg for optional session recording
        - Guest helper scripts for starting/stopping browser sessions
        """
        if not self.check_docker():
            raise self.docker_requirement_error()

        key_value = self._resolve_public_key(ssh_public_key)

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        resolved_kernel_url = self._resolve_kernel_url(kernel_profile, kernel_url)

        init_script = self._default_init_script()

        browser_session_sh = r"""#!/bin/sh
set -eu

RUNTIME_DIR=/run/smolvm-browser
LOG_DIR=/var/log/smolvm-browser
NOVNC_WEB_ROOT=/usr/share/novnc

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"

find_browser_bin() {
    for candidate in chromium chromium-browser /usr/bin/chromium /usr/bin/chromium-browser; do
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    echo "Chromium binary not found" >&2
    return 1
}

stop_pid_file() {
    pid_file="$1"
    if [ -f "$pid_file" ]; then
        pid="$(cat "$pid_file" 2>/dev/null || true)"
        if [ -n "${pid}" ]; then
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$pid_file"
    fi
}

write_preferences() {
    profile_dir="$1"
    download_dir="$2"
    python3 - "$profile_dir" "$download_dir" <<'PY'
import json
import pathlib
import sys

profile_dir = pathlib.Path(sys.argv[1])
download_dir = pathlib.Path(sys.argv[2])
default_dir = profile_dir / "Default"
default_dir.mkdir(parents=True, exist_ok=True)

preferences = {
    "browser": {
        "check_default_browser": False,
    },
    "distribution": {
        "import_bookmarks": False,
        "skip_first_run_ui": True,
    },
    "download": {
        "default_directory": str(download_dir),
        "directory_upgrade": True,
        "prompt_for_download": False,
    },
    "profile": {
        "default_content_setting_values": {
            "notifications": 2,
        },
    },
}

(default_dir / "Preferences").write_text(json.dumps(preferences))
PY
}

start_live_stack() {
    width="$1"
    height="$2"
    live_port="$3"
    record_video="$4"
    artifacts_dir="$5"

    mkdir -p "$artifacts_dir"

    nohup Xvfb :99 -screen 0 "${width}x${height}x24" \
        >"${LOG_DIR}/xvfb.log" 2>&1 &
    echo $! >"${RUNTIME_DIR}/xvfb.pid"

    DISPLAY=:99 HOME=/root nohup openbox \
        >"${LOG_DIR}/openbox.log" 2>&1 &
    echo $! >"${RUNTIME_DIR}/openbox.pid"

    nohup x11vnc -display :99 -nopw -forever -shared -rfbport 5900 \
        >"${LOG_DIR}/x11vnc.log" 2>&1 &
    echo $! >"${RUNTIME_DIR}/x11vnc.pid"

    nohup websockify --web="${NOVNC_WEB_ROOT}" "${live_port}" 127.0.0.1:5900 \
        >"${LOG_DIR}/websockify.log" 2>&1 &
    echo $! >"${RUNTIME_DIR}/websockify.pid"

    if [ "${record_video}" = "1" ]; then
        nohup ffmpeg -y -video_size "${width}x${height}" -framerate 12 \
            -f x11grab -i :99 -codec:v libx264 -preset ultrafast \
            "${artifacts_dir}/session.mp4" \
            >"${LOG_DIR}/ffmpeg.log" 2>&1 &
        echo $! >"${RUNTIME_DIR}/ffmpeg.pid"
    fi
}

stop_session() {
    stop_pid_file "${RUNTIME_DIR}/ffmpeg.pid"
    stop_pid_file "${RUNTIME_DIR}/websockify.pid"
    stop_pid_file "${RUNTIME_DIR}/x11vnc.pid"
    stop_pid_file "${RUNTIME_DIR}/openbox.pid"
    stop_pid_file "${RUNTIME_DIR}/xvfb.pid"
    stop_pid_file "${RUNTIME_DIR}/chromium.pid"
}

start_session() {
    mode="$1"
    width="$2"
    height="$3"
    debug_port="$4"
    live_port="$5"
    profile_dir="$6"
    download_dir="$7"
    record_video="$8"
    downloads_enabled="$9"
    artifacts_dir="${10}"

    browser_bin="$(find_browser_bin)"

    mkdir -p "$profile_dir" "$download_dir" "$artifacts_dir"
    if [ "${downloads_enabled}" = "1" ]; then
        chmod 700 "$download_dir"
    else
        chmod 500 "$download_dir"
    fi

    write_preferences "$profile_dir" "$download_dir"
    stop_session

    if [ "${mode}" = "live" ]; then
        start_live_stack "$width" "$height" "$live_port" "$record_video" "$artifacts_dir"
    fi

    if [ "${mode}" = "headless" ]; then
        nohup "$browser_bin" \
            --headless=new \
            --no-sandbox \
            --disable-dev-shm-usage \
            --disable-gpu \
            --no-first-run \
            --no-default-browser-check \
            --disable-background-networking \
            --disable-component-update \
            --metrics-recording-only \
            --password-store=basic \
            --use-mock-keychain \
            --remote-allow-origins=* \
            --remote-debugging-address=0.0.0.0 \
            --remote-debugging-port="${debug_port}" \
            --user-data-dir="${profile_dir}" \
            --window-size="${width},${height}" \
            about:blank \
            >"${LOG_DIR}/chromium.log" 2>&1 &
    else
        DISPLAY=:99 HOME=/root nohup "$browser_bin" \
            --no-sandbox \
            --disable-dev-shm-usage \
            --disable-gpu \
            --no-first-run \
            --no-default-browser-check \
            --disable-background-networking \
            --disable-component-update \
            --metrics-recording-only \
            --password-store=basic \
            --use-mock-keychain \
            --remote-allow-origins=* \
            --remote-debugging-address=0.0.0.0 \
            --remote-debugging-port="${debug_port}" \
            --user-data-dir="${profile_dir}" \
            --window-size="${width},${height}" \
            about:blank \
            >"${LOG_DIR}/chromium.log" 2>&1 &
    fi

    echo $! >"${RUNTIME_DIR}/chromium.pid"
}

case "${1:-}" in
    start)
        if [ "$#" -ne 11 ]; then
            echo "usage: smolvm-browser-session start <mode> <width> <height>" >&2
            echo "  <debug_port> <live_port> <profile_dir> <download_dir>" >&2
            echo "  <record_video> <downloads_enabled> <artifacts_dir>" >&2
            exit 2
        fi
        shift
        start_session "$@"
        ;;
    stop)
        stop_session
        ;;
    *)
        echo "usage: smolvm-browser-session {start|stop}" >&2
        exit 2
        ;;
esac
"""

        wait_port_py = r"""#!/usr/bin/env python3
import socket
import sys
import time

if len(sys.argv) != 3:
    raise SystemExit("usage: smolvm-browser-wait-port <port> <timeout_seconds>")

port = int(sys.argv[1])
timeout = float(sys.argv[2])
deadline = time.time() + timeout

while time.time() < deadline:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise SystemExit(0)
    time.sleep(0.5)

raise SystemExit(1)
"""

        dockerfile_content = f"""
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server \\
    iproute2 \\
    curl \\
    bash \\
    ca-certificates \\
    python3 \\
    chromium \\
    xvfb \\
    x11vnc \\
    novnc \\
    websockify \\
    openbox \\
    ffmpeg \\
    fonts-dejavu-core \\
    fonts-liberation \\
    dbus-x11 \\
    xauth \\
    procps \\
    tar \\
    && rm -rf /var/lib/apt/lists/*

# Host keys generated at first boot in /init so each VM has unique identity.
# 'rm -f' purges keys planted by the openssh-server postinst on Debian.
RUN rm -f /etc/ssh/ssh_host_* && \\
    mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh && \\
    sed -ri 's/^#?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

RUN mkdir -p \\
    /opt/smolvm-browser/profiles \\
    /opt/smolvm-browser/downloads \\
    /opt/smolvm-browser/artifacts

COPY smolvm-browser-session /usr/local/bin/smolvm-browser-session
COPY smolvm-browser-wait-port /usr/local/bin/smolvm-browser-wait-port
RUN chmod +x /usr/local/bin/smolvm-browser-session /usr/local/bin/smolvm-browser-wait-port

COPY init /init
RUN chmod +x /init
"""

        # ssh_public_key is intentionally not in the fingerprint and not baked
        # into the rootfs — see VMConfig.ssh_public_key + /init parser.
        _ = key_value
        fingerprint_data = self._fingerprint_with_content(
            {
                "rootfs_size_mb": rootfs_size_mb,
                "kernel_url": resolved_kernel_url,
                "kernel_profile": kernel_profile.value,
                "base_image": base_image,
                "image_type": "browser-chromium-v3",
                "_browser_session_sha256": hashlib.sha256(browser_session_sh.encode()).hexdigest(),
                "_wait_port_sha256": hashlib.sha256(wait_port_py.encode()).hexdigest(),
            },
            dockerfile_content,
            init_script,
        )

        if kernel_path.exists() and rootfs_path.exists():
            if self._check_fingerprint(image_dir, fingerprint_data):
                logger.info(
                    "Image '%s' already exists and fingerprint matches at %s", name, image_dir
                )
                return (kernel_path, rootfs_path)

            logger.info("Inputs changed for browser image '%s'. Rebuilding...", name)
            kernel_path.unlink(missing_ok=True)
            rootfs_path.unlink(missing_ok=True)

        logger.info("Building browser image '%s'...", name)
        image_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._do_build(
                name,
                dockerfile_content,
                init_script,
                image_dir,
                kernel_path,
                rootfs_path,
                rootfs_size_mb,
                extra_files={
                    "smolvm-browser-session": browser_session_sh,
                    "smolvm-browser-wait-port": wait_port_py,
                },
                kernel_url=resolved_kernel_url,
                fingerprint_data=fingerprint_data,
            )
        except (subprocess.CalledProcessError, ImageError) as e:
            if rootfs_path.exists():
                rootfs_path.unlink()
            if kernel_path.exists():
                kernel_path.unlink()
            if isinstance(e, ImageError):
                raise
            raise ImageError(f"Image build failed: {e}") from e

        logger.info("Image '%s' built successfully at %s", name, image_dir)
        return (kernel_path, rootfs_path)

    def build_openclaw_rootfs(
        self,
        name: str = "openclaw",
        ssh_public_key: str | Path | None = None,
        rootfs_size_mb: int = 2048,
        kernel_url: str | None = None,
        extra_packages: list[str] | None = None,
    ) -> tuple[Path, Path]:
        """Build OpenClaw rootfs with Node.js, sidecars, and init wiring.

        The resulting image contains:

        - Node.js >= 22.12.0 (``node:22.12.0-bookworm-slim`` base)
        - OpenClaw pre-installed at ``/opt/openclaw/`` (symlinked to ``/usr/local/bin/openclaw``)
        - ``inotify-tools`` and the device-approver sidecar
        - SSH server for ``vm.run()`` management commands
        - Custom ``/init`` that boots networking, sshd, and the sidecar
        - Custom system packages like `git` (for npm source dependencies)

        Boot the resulting VM with :data:`OPENCLAW_BOOT_ARGS`.

        Args:
            name: Image name for caching.
            ssh_public_key: Public key content or path to a public key file.
            rootfs_size_mb: Size of rootfs in MB (default: 2048).
            kernel_url: Optional kernel URL override.
            extra_packages: List of apt packages to install (defaults to ['git']).

        Returns:
            Tuple of (kernel_path, rootfs_path).

        Raises:
            ImageError: If Docker is not available or build fails.
        """
        if not self.check_docker():
            raise self.docker_requirement_error()

        if extra_packages is None:
            extra_packages = ["git"]

        # Validate package names to prevent Dockerfile string-interpolation injection
        valid_pkg_regex = re.compile(r"^[a-z0-9\.\+\-]+$")
        for pkg in extra_packages:
            if not valid_pkg_regex.match(pkg):
                raise ImageError(f"Invalid package name requested for installation: '{pkg}'")

        if ssh_public_key is None:
            key_path = Path.home() / ".smolvm" / "keys" / "id_ed25519.pub"
            try:
                key_value = key_path.read_text().strip()
            except OSError:
                key_value = ""
        else:
            key_value = self._resolve_public_key(ssh_public_key)

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        packages_str = " ".join(extra_packages)

        init_script = self._openclaw_init_script()

        # --- Sidecar scripts (TDD Decision 1.2.5) ---
        device_approver_py = r"""#!/usr/bin/env python3
import json, time

BASE = "/home/node/.openclaw/devices"
PENDING = f"{BASE}/pending.json"
PAIRED  = f"{BASE}/paired.json"

def approve():
    try:
        pending = json.loads(open(PENDING).read())
    except (FileNotFoundError, json.JSONDecodeError):
        return
    if not pending:
        return
    try:
        paired = json.loads(open(PAIRED).read())
    except (FileNotFoundError, json.JSONDecodeError):
        paired = {}
    now_ms = int(time.time() * 1000)
    for _, entry in pending.items():
        device_id = entry.get("deviceId")
        if not device_id:
            continue
        paired[device_id] = {**entry, "pairedAt": now_ms}
    open(PAIRED, "w").write(json.dumps(paired, indent=2))
    open(PENDING, "w").write(json.dumps({}))

approve()
"""

        watch_devices_sh = r"""#!/bin/bash
# Watch DIRECTORY not the file — handles atomic rename writes
while inotifywait -e close_write,moved_to \
    /home/node/.openclaw/devices 2>/dev/null; do
    python3 /usr/local/bin/device-approver.py
done
"""

        systemctl_proxy_sh = r"""#!/bin/bash
if [ "$1" = "start" ] && [ "$2" = "openclaw" ]; then
    echo "Starting openclaw via dummy systemctl..."
    # The reconciler provisions the config via SSH then calls `systemctl start openclaw`.
    # We use --allow-unconfigured so the gateway starts even before pairing completes.
    #
    # `</dev/null` — prevents openclaw from inheriting the SSH channel's stdin, which
    #   would otherwise keep the SSH session alive until openclaw exits.
    # `& disown`   — removes the job from bash's job table so the non-interactive shell
    #   (su -c) can exit immediately without waiting for the backgrounded process.
    #   Without disown, non-interactive bash may wait for child jobs before exiting,
    #   causing the reconciler's ssh timeout to fire even though openclaw started.
    _CMD="cd /home/node && HOME=/home/node"
    _CMD="${_CMD} nohup openclaw gateway --allow-unconfigured"
    _CMD="${_CMD} </dev/null > /var/log/openclaw.log 2>&1 & disown"
    su - node -c "${_CMD}"
    exit 0
fi
echo "dummy systemctl: ignoring command $@"
exit 0
"""

        dockerfile_content = f"""
FROM node:22.12.0-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \\
    openssh-server \\
    iproute2 \\
    curl \\
    bash \\
    ca-certificates \\
    inotify-tools \\
    python3 \\
    {packages_str} \\
    && rm -rf /var/lib/apt/lists/*

# Host keys generated at first boot in /init so each VM has unique identity.
# 'rm -f' purges keys planted by the openssh-server postinst on Debian.
RUN rm -f /etc/ssh/ssh_host_* && \\
    mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh && \\
    sed -ri 's/^#?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

# Prepare OpenClaw directories and workspace
RUN useradd -m -s /bin/bash node 2>/dev/null || true && \\
    mkdir -p /opt/openclaw /home/node/.openclaw/devices /workspace && \\
    chown -R node:node /opt/openclaw /home/node/.openclaw /workspace

WORKDIR /opt/openclaw
RUN npm init -y && \\
    npm --prefix /opt/openclaw install -g openclaw && \\
    ln -sf /opt/openclaw/bin/openclaw /usr/local/bin/openclaw && \\
    touch /var/log/openclaw.log && \\
    chown node:node /var/log/openclaw.log && \\
    npm cache clean --force >/dev/null 2>&1 || true

# Strip @node-llama-cpp GPU and non-host-arch backends. Inside Firecracker
# there is no GPU passthrough, so the CUDA/Vulkan binaries are dead weight
# (~600+ MiB on amd64). On arm64 only the matching arch package exists, so
# the rm calls are no-ops there. The path reflects npm's `-g --prefix`
# layout: /opt/openclaw/lib/node_modules/openclaw/node_modules/...
RUN rm -rf \\
    /opt/openclaw/lib/node_modules/openclaw/node_modules/@node-llama-cpp/linux-x64-cuda \\
    /opt/openclaw/lib/node_modules/openclaw/node_modules/@node-llama-cpp/linux-x64-cuda-ext \\
    /opt/openclaw/lib/node_modules/openclaw/node_modules/@node-llama-cpp/linux-x64-vulkan \\
    /opt/openclaw/lib/node_modules/openclaw/node_modules/@node-llama-cpp/linux-armv7l

# Sidecar and proxy scripts
COPY device-approver.py /usr/local/bin/device-approver.py
COPY watch-devices.sh /usr/local/bin/watch-devices.sh
COPY systemctl /usr/local/bin/systemctl
RUN chmod +x \\
    /usr/local/bin/device-approver.py \\
    /usr/local/bin/watch-devices.sh \\
    /usr/local/bin/systemctl

# Init script
COPY init /init
RUN chmod +x /init
"""

        # ssh_public_key is intentionally not in the fingerprint and not baked
        # into the rootfs — see VMConfig.ssh_public_key + /init parser.
        _ = key_value
        fingerprint_data = self._fingerprint_with_content(
            {
                "rootfs_size_mb": rootfs_size_mb,
                "kernel_url": kernel_url,
                "extra_packages": extra_packages,
                "_device_approver_sha256": hashlib.sha256(device_approver_py.encode()).hexdigest(),
                "_watch_devices_sha256": hashlib.sha256(watch_devices_sh.encode()).hexdigest(),
                "_systemctl_proxy_sha256": hashlib.sha256(systemctl_proxy_sh.encode()).hexdigest(),
            },
            dockerfile_content,
            init_script,
        )

        if kernel_path.exists() and rootfs_path.exists():
            if self._check_fingerprint(image_dir, fingerprint_data):
                logger.info(
                    "Image '%s' already exists and fingerprint matches at %s", name, image_dir
                )
                return (kernel_path, rootfs_path)

            logger.info("Inputs changed for OpenClaw image '%s'. Rebuilding...", name)
            kernel_path.unlink(missing_ok=True)
            rootfs_path.unlink(missing_ok=True)

        logger.info("Building OpenClaw image '%s' with extra packages: %s...", name, packages_str)
        image_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._do_build(
                name,
                dockerfile_content,
                init_script,
                image_dir,
                kernel_path,
                rootfs_path,
                rootfs_size_mb,
                extra_files={
                    "device-approver.py": device_approver_py,
                    "watch-devices.sh": watch_devices_sh,
                    "systemctl": systemctl_proxy_sh,
                },
                kernel_url=kernel_url,
                fingerprint_data=fingerprint_data,
            )
        except (subprocess.CalledProcessError, ImageError) as e:
            if rootfs_path.exists():
                rootfs_path.unlink()
            if kernel_path.exists():
                kernel_path.unlink()
            if isinstance(e, ImageError):
                raise
            raise ImageError(f"Image build failed: {e}") from e

        logger.info("Image '%s' built successfully at %s", name, image_dir)
        return (kernel_path, rootfs_path)

    def _resolve_public_key(self, ssh_public_key: str | Path) -> str:
        """Resolve a public key from inline content or file path."""
        key_text = str(ssh_public_key).strip()
        key_path = Path(key_text)
        if key_path.exists():
            key_text = key_path.read_text().strip()
        if not key_text.startswith("ssh-"):
            raise ImageError("Invalid SSH public key format")
        return key_text

    def _base_init_script(self, custom_hostname: str = "smolvm", custom_commands: str = "") -> str:
        """Base PID 1 init script used by SSH-capable images.

        Args:
            custom_hostname: Hostname to set (default: smolvm).
            custom_commands: Additional shell commands to inject before the PID 1 sleep loop.
        """
        return f"""#!/bin/sh
# SmolVM custom init - runs as PID 1 inside Firecracker VM

# ── Signal handling ──────────────────────────────────────────
# Firecracker's SendCtrlAltDel sends Ctrl+Alt+Del to the guest
# kernel.  By default the kernel handles this by calling
# kernel_restart() which tries a hardware reboot (doesn't exist
# in Firecracker, so the VM hangs).  We disable CAD so the
# kernel sends SIGINT to PID 1 instead, where we trap it.
shutdown() {{
    echo "SmolVM init: shutting down..."
    kill -TERM -1 2>/dev/null
    sleep 0.2
    sync
    poweroff -f
}}
trap shutdown INT TERM PWR

# ── Timestamp helpers (for host-side startup profiling) ──────
ts_uptime() {{
    cut -d' ' -f1 /proc/uptime 2>/dev/null || echo "0.00"
}}

# date +%s is widely supported by busybox/coreutils.
ts_epoch() {{
    date +%s 2>/dev/null || echo "0"
}}

log_ts() {{
    STAGE="$1"
    echo "SMOLVM_TS stage=${{STAGE}} epoch_s=$(ts_epoch) uptime_s=$(ts_uptime)"
}}

log_ts "init-start"

# ── Mount essential filesystems ──────────────────────────────
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev 2>/dev/null  # may already be mounted
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /run
# Keep /tmp on the root disk, not tmpfs. Package managers use /tmp for
# temporary writes, and a memory-sized tmpfs can fill up even when the disk
# still has plenty of space.

log_ts "mounts-ready"

# Disable Ctrl+Alt+Del hardware reboot — send SIGINT to PID 1 instead
echo 0 > /proc/sys/kernel/ctrl-alt-del

# Remount root read-write
mount -o remount,rw /

# Create required directories
mkdir -p /run/sshd /var/log /tmp
chmod 1777 /tmp

log_ts "root-ready"

# ── Networking ───────────────────────────────────────────────
log_ts "net-config-start"
# Configure from kernel command line ip= parameter
# Format: ip=<guest_ip>::<gateway>:<netmask>::eth0:off
netmask_to_prefix() {{
    IFS=.
    set -- $1
    IFS=' '

    [ $# -eq 4 ] || return 1

    PREFIX=0
    ZERO_SEEN=0
    for OCTET in "$@"; do
        case "$OCTET" in
            255) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 8)) ;;
            254) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 7)); ZERO_SEEN=1 ;;
            252) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 6)); ZERO_SEEN=1 ;;
            248) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 5)); ZERO_SEEN=1 ;;
            240) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 4)); ZERO_SEEN=1 ;;
            224) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 3)); ZERO_SEEN=1 ;;
            192) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 2)); ZERO_SEEN=1 ;;
            128) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 1)); ZERO_SEEN=1 ;;
            0) ZERO_SEEN=1 ;;
            *) return 1 ;;
        esac
    done

    echo "$PREFIX"
}}

IP_CONFIG=$(cat /proc/cmdline | tr ' ' '\n' | grep '^ip=' | head -1)
if [ -n "$IP_CONFIG" ]; then
    IP_FIELDS=$(echo "$IP_CONFIG" | cut -d= -f2-)
    GUEST_IP=$(echo "$IP_FIELDS" | cut -d: -f1)
    GATEWAY=$(echo "$IP_FIELDS" | cut -d: -f3)
    NETMASK=$(echo "$IP_FIELDS" | cut -d: -f4)
else
    GUEST_IP="172.16.0.2"
    GATEWAY="172.16.0.1"
    NETMASK="255.255.255.0"
fi

PREFIX=$(netmask_to_prefix "$NETMASK") || PREFIX=24

ip link set lo up
ip link set eth0 up
ip addr add "${{GUEST_IP}}/${{PREFIX}}" dev eth0 2>/dev/null || true
ip route add default via "${{GATEWAY}}" dev eth0 2>/dev/null || true

# DNS
if [ -n "$GATEWAY" ]; then
    echo "nameserver ${{GATEWAY}}" > /etc/resolv.conf
    echo "nameserver 8.8.8.8" >> /etc/resolv.conf
    echo "nameserver 8.8.4.4" >> /etc/resolv.conf
else
    echo "nameserver 8.8.8.8" > /etc/resolv.conf
    echo "nameserver 8.8.4.4" >> /etc/resolv.conf
fi

hostname {custom_hostname}
log_ts "net-ready"

# ── Guest agent (vsock control plane) ───────────────────────
# Started before sshd and independent of networking, so the host can
# drive the guest over vsock the moment the kernel is up. Skipped
# silently if the image has no python3 or the agent wasn't baked in —
# the host falls back to SSH in that case.
log_ts "guest-agent-start"
if command -v python3 >/dev/null 2>&1 && [ -f /usr/local/bin/smolvm-guest-agent ]; then
    python3 /usr/local/bin/smolvm-guest-agent >/var/log/smolvm-agent.log 2>&1 &
    echo "SmolVM init: guest agent started (PID=$!)"
else
    echo "SmolVM init: guest agent not started (python3 or agent missing)"
fi
log_ts "guest-agent-started"

# ── SSH ──────────────────────────────────────────────────────
log_ts "ssh-hostkey-check-start"
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
    echo "SmolVM init: SSH host keys missing; generating..."
    ssh-keygen -A 2>/dev/null
fi
log_ts "ssh-hostkey-check-done"

# Pull authorized_keys from the kernel cmdline if the host injected one.
# Format: smolvm.authorized_key_b64=<base64-of-the-pubkey-line>. Used for
# published images that don't bake keys at build time, so each VM gets the
# launching user's key without rebuilding the rootfs.
log_ts "ssh-authkey-inject-start"
AUTHKEY_B64=$(cat /proc/cmdline | tr ' ' '\n' \
    | grep '^smolvm\\.authorized_key_b64=' | head -1 | cut -d= -f2-)
if [ -n "$AUTHKEY_B64" ]; then
    DECODED=$(echo "$AUTHKEY_B64" | base64 -d 2>/dev/null)
    if [ -n "$DECODED" ]; then
        mkdir -p /root/.ssh
        chmod 700 /root/.ssh
        echo "$DECODED" > /root/.ssh/authorized_keys
        chmod 600 /root/.ssh/authorized_keys
        echo "SmolVM init: installed authorized_keys from cmdline"
    else
        echo "SmolVM init: smolvm.authorized_key_b64 present but failed to decode"
    fi
fi
log_ts "ssh-authkey-inject-done"

log_ts "sshd-start"
/usr/sbin/sshd -e
log_ts "sshd-invoked"

echo "SmolVM init complete: IP=${{GUEST_IP}}, SSH listening on port 22"
log_ts "init-complete"

# ── Custom Injections ───────────────────────────────────────
{custom_commands}

# ── Keep PID 1 alive ────────────────────────────────────────
# Use 'wait' so signals are delivered promptly (plain 'sleep'
# in a while-loop prevents signal delivery until sleep exits).
while true; do
    sleep 3600 &
    wait $!
done
"""

    def _default_init_script(self) -> str:
        """Default PID 1 init script used by SSH-capable images."""
        return self._base_init_script()

    def _openclaw_init_script(self) -> str:
        """PID 1 init script for OpenClaw images.

        Extends the base init with:
        - Device-approver sidecar launched as a background process
        - ``/home/node/.openclaw/devices`` directory setup
        - Hostname set to ``openclaw``
        """
        device_approver_block = r"""
# ── Device-Approver Sidecar ─────────────────────────────────
# Launched as a background process — no systemd required.
# watch-devices.sh uses inotifywait on the directory (not file) to
# handle atomic-rename writes from OpenClaw.
log_ts "device-approver-start"
mkdir -p /home/node/.openclaw/devices
chown -R 1000:1000 /home/node/.openclaw
/usr/local/bin/watch-devices.sh &
DEVICE_APPROVER_PID=$!
log_ts "device-approver-started"
echo "Device-approver running with PID=${DEVICE_APPROVER_PID}"
"""
        return self._base_init_script(
            custom_hostname="openclaw", custom_commands=device_approver_block
        )

    def _loopfs_helper_path(self) -> Path | None:
        """Return installed privileged helper path if available."""
        if LOOPFS_HELPER_PATH.is_file():
            return LOOPFS_HELPER_PATH
        return None

    def _run_loopfs(self, action: str, *args: Path, timeout: int = 30) -> None:
        """Run a privileged loopfs action through the scoped helper.

        Args:
            action: One of ``mount``, ``extract``, ``umount``.
            *args: Positional path arguments forwarded to the helper.
            timeout: Command timeout in seconds.  Mount/umount are fast
                (default 30 s); callers should pass a larger value for
                ``extract`` when working with large images.
        """
        helper = self._loopfs_helper_path()
        if helper is None:
            raise ImageError(
                "Missing loopfs helper for image building.\n"
                f"Expected helper at: {LOOPFS_HELPER_PATH}\n"
                f"{RUNTIME_PRIVILEGE_SETUP_HINT}"
            )

        cmd = [str(helper), action, *(str(arg) for arg in args)]
        try:
            run_command(cmd, use_sudo=True, check=True, capture_output=True, timeout=timeout)
        except SmolVMError as e:
            raise ImageError(
                "Image build loopfs operation failed.\n"
                f"Command: {' '.join(cmd)}\n"
                f"{RUNTIME_PRIVILEGE_SETUP_HINT}\n"
                f"error: {e}"
            ) from e

    @staticmethod
    def _host_arch_key() -> str:
        """Normalize host architecture to SmolVM kernel key."""
        arch = platform.machine().lower()
        try:
            return normalize_arch(arch)
        except ValueError as exc:
            raise ImageError(str(exc)) from exc

    def _kernel_url_for_host(self) -> str:
        """Return the SmolVM-built base kernel URL for the current host arch.

        Defaults to the ELF format (Firecracker-compatible). Callers that
        need the Image format must pass an explicit ``kernel_url`` override.
        """
        return self._resolve_kernel_url(KernelBootProfile.MICROVM_DIRECT)

    def qemu_kernel_url_for_host(self) -> str:
        """Return the SmolVM-built Image-format base kernel URL.

        Retained for back-compat with callers that branched on boot profile;
        returns the ``.image`` artifact (QEMU-compatible).
        """
        from smolvm.images.published import BASE_KERNELS

        smolvm_arch = "amd64" if self._host_arch_key() == "x86_64" else "arm64"
        return BASE_KERNELS[smolvm_arch].image_url

    def _resolve_kernel_url(
        self,
        kernel_profile: KernelBootProfile,  # noqa: ARG002 (kept for back-compat)
        kernel_url: str | None = None,
    ) -> str:
        """Return the effective kernel URL for an image build.

        Defaults to the ELF artifact (Firecracker-compatible). Callers wanting
        the Image format pass it explicitly via ``kernel_url`` — typically
        derived in ``_build_auto_config`` from ``BASE_KERNELS[arch].url_for(fmt)``
        based on the resolved backend.
        """
        if kernel_url is not None:
            return kernel_url
        from smolvm.images.published import BASE_KERNELS

        smolvm_arch = "amd64" if self._host_arch_key() == "x86_64" else "arm64"
        return BASE_KERNELS[smolvm_arch].elf_url

    def _fingerprint_with_content(
        self,
        fingerprint_data: dict[str, typing.Any],
        dockerfile_content: str,
        init_script: str,
    ) -> dict[str, typing.Any]:
        """Augment the input-only fingerprint with Dockerfile and init-script hashes.

        Without this, edits to the Dockerfile or /init script are invisible to
        the fingerprint check — the cache keeps serving the old image even
        though the build recipe has changed.
        """
        return {
            **fingerprint_data,
            "_dockerfile_sha256": hashlib.sha256(dockerfile_content.encode()).hexdigest(),
            "_init_script_sha256": hashlib.sha256(init_script.encode()).hexdigest(),
            # The guest agent is injected into every image by _do_build, so an
            # edit to it must invalidate all cached images.
            "_guest_agent_sha256": hashlib.sha256(_guest_agent_source().encode()).hexdigest(),
        }

    def _check_fingerprint(self, image_dir: Path, data: dict[str, typing.Any]) -> bool:
        """Check if the cached image fingerprint matches the current build inputs."""
        fingerprint_file = image_dir / ".fingerprint"
        if not fingerprint_file.exists():
            return False

        expected_hash = self._hash_fingerprint_data(data)
        try:
            stored_hash = fingerprint_file.read_text().strip()
            return stored_hash == expected_hash
        except OSError:
            return False

    def _write_fingerprint(self, image_dir: Path, data: dict[str, typing.Any]) -> None:
        """Write the build input fingerprint to the cache directory."""
        fingerprint_file = image_dir / ".fingerprint"
        try:
            fingerprint_file.write_text(self._hash_fingerprint_data(data))
        except OSError as e:
            logger.warning("Failed to write image fingerprint cache: %s", e)

    def _hash_fingerprint_data(self, data: dict[str, typing.Any]) -> str:
        """Compute SHA-256 hash of a JSON-serializable dictionary."""
        json_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(json_str.encode("utf-8")).hexdigest()

    def _download_kernel(self, url: str, dest: Path) -> None:
        """Download kernel image to *dest*.

        For SmolVM's published base kernel (the default path) we route through
        :func:`smolvm.images.published.ensure_base_kernel`, which downloads to
        a shared cache and SHA-256-verifies the artifact. Custom override URLs
        fall back to a direct urllib fetch (no SHA — the override is the
        caller's responsibility).
        """
        from smolvm.images.published import BASE_KERNELS, ensure_base_kernel

        # Recognise our own published kernel URLs (either format) and use the
        # SHA-verified cached path.
        for arch, entry in BASE_KERNELS.items():
            if url == entry.elf_url:
                cached = ensure_base_kernel(arch, "elf")
                shutil.copy2(cached, dest)
                return
            if url == entry.image_url:
                cached = ensure_base_kernel(arch, "image")
                shutil.copy2(cached, dest)
                return

        try:
            with (
                urllib.request.urlopen(url, timeout=180) as response,
                open(dest, "wb") as out,
            ):
                shutil.copyfileobj(response, out)
        except (urllib.error.URLError, OSError) as e:
            raise ImageError(f"Failed to download kernel from {url}: {e}") from e

    def _create_ext4_with_loopfs(
        self,
        tar_path: Path,
        rootfs_path: Path,
        rootfs_size_mb: int,
        tmpdir: Path,
    ) -> None:
        """Create and populate ext4 rootfs via loopfs helper (Linux path)."""
        logger.info("  [3/4] Creating ext4 filesystem (%dMB)...", rootfs_size_mb)
        subprocess.run(
            ["dd", "if=/dev/zero", f"of={rootfs_path}", "bs=1M", f"count={rootfs_size_mb}"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["mkfs.ext4", "-F", str(rootfs_path)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        mount_dir = tmpdir / "mnt"
        mount_dir.mkdir()
        self._run_loopfs("mount", rootfs_path, mount_dir)

        # Scale extract timeout with image size: tar-extracting thousands of
        # Node.js module files onto a loop-mounted ext4 is inode-bound, not
        # throughput-bound.  30 s is sufficient for mount/umount but far too
        # short for a 4 GB+ rootfs on a standard (non-SSD) disk.
        extract_timeout = max(300, rootfs_size_mb // 8)
        tar_error: Exception | None = None
        try:
            self._run_loopfs("extract", tar_path, mount_dir, timeout=extract_timeout)
        except Exception as e:
            tar_error = e
        finally:
            try:
                self._run_loopfs("umount", mount_dir, timeout=extract_timeout)
            except ImageError:
                if tar_error is None:
                    raise
                logger.warning(
                    "Failed to unmount rootfs after tar extraction error",
                    exc_info=True,
                )
        if tar_error is not None:
            raise tar_error

    def _create_ext4_with_docker(
        self,
        tar_path: Path,
        rootfs_path: Path,
        rootfs_size_mb: int,
        tmpdir: Path,
    ) -> None:
        """Create and populate ext4 rootfs using Docker + mke2fs.

        This path avoids Linux loop mounts and works on macOS where
        ``mkfs.ext4`` and loop devices are typically unavailable.
        """
        logger.info("  [3/4] Creating ext4 filesystem via Docker helper (%dMB)...", rootfs_size_mb)

        # Validate tar member paths up-front so a malicious docker export
        # can't trick the in-container extraction either. Python's tarfile
        # is the cheapest tool for the security check; the actual extract
        # happens below as root inside the container so file uids/gids are
        # preserved (extracting on the host as the unprivileged runner uid
        # silently rewrites everything to that uid, then mke2fs -d bakes
        # those bogus uids into the ext4 inodes — was a real bug).
        with tarfile.open(tar_path, "r") as tar:
            for member in tar.getmembers():
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ImageError(
                        f"Refusing to extract suspicious tar path from docker export: {member.name}"
                    )

        rootfs_path.unlink(missing_ok=True)
        rootfs_name = shlex.quote(rootfs_path.name)
        shell_cmd = (
            "set -e; "
            "apk add --no-cache e2fsprogs tar >/dev/null; "
            "mkdir -p /work/rootfs-staging; "
            "tar -xf /work/in/rootfs.tar -C /work/rootfs-staging; "
            f"mke2fs -d /work/rootfs-staging -t ext4 -F /work/out/{rootfs_name} "
            f"{rootfs_size_mb}M >/dev/null"
        )

        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{tar_path.resolve()}:/work/in/rootfs.tar:ro",
                    "-v",
                    f"{rootfs_path.parent.resolve()}:/work/out",
                    "alpine:3.19",
                    "sh",
                    "-lc",
                    shell_cmd,
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            raise ImageError(
                "Failed to create ext4 image via Docker helper.\n"
                f"Command: docker run ... tar | mke2fs\n"
                f"stderr: {stderr}"
            ) from e

        if not rootfs_path.exists():
            raise ImageError(f"Expected rootfs image not produced: {rootfs_path}")

    def _do_build(
        self,
        name: str,
        dockerfile_content: str,
        init_script: str,
        image_dir: Path,
        kernel_path: Path,
        rootfs_path: Path,
        rootfs_size_mb: int,
        extra_files: dict[str, str] | None = None,
        build_args: dict[str, str] | None = None,
        kernel_url: str | None = None,
        fingerprint_data: dict[str, typing.Any] | None = None,
    ) -> None:
        """Execute the Docker build and image conversion."""
        docker_tag = f"smolvm-{name}"

        # Bake the guest agent into every image. Centralized here so all five
        # build_* recipes inherit it without each repeating the COPY; /init
        # launches it (guarded on python3), and the host reaches it over vsock.
        # Appended after the recipe's own COPY lines — order is irrelevant for
        # an independent file drop. Its content hash is in the fingerprint via
        # _fingerprint_with_content, so edits still trigger a rebuild even
        # though this COPY text is constant.
        dockerfile_content = (
            dockerfile_content
            + "\n# SmolVM guest agent (vsock control plane)\n"
            + f"COPY {_GUEST_AGENT_BUILD_FILE} {_GUEST_AGENT_GUEST_PATH}\n"
            + f"RUN chmod +x {_GUEST_AGENT_GUEST_PATH}\n"
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Write Dockerfile and init script
            (tmp_path / "Dockerfile").write_text(dockerfile_content)
            (tmp_path / "init").write_text(init_script)
            (tmp_path / _GUEST_AGENT_BUILD_FILE).write_text(_guest_agent_source())
            if extra_files:
                for filename, content in extra_files.items():
                    (tmp_path / filename).write_text(content)

            # 1. Build Docker image
            logger.info("  [1/4] Building Docker image...")
            build_cmd = ["docker", "build", "-t", docker_tag]
            if build_args:
                for k, v in build_args.items():
                    build_cmd.extend(["--build-arg", f"{k}={v}"])
            build_cmd.append(str(tmp_path))

            subprocess.run(
                build_cmd,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            # 2. Export rootfs from container
            logger.info("  [2/4] Exporting rootfs...")
            container_id = subprocess.run(
                ["docker", "create", docker_tag],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            try:
                tar_path = tmp_path / "rootfs.tar"
                subprocess.run(
                    ["docker", "export", container_id, "-o", str(tar_path)],
                    check=True,
                )
            finally:
                subprocess.run(
                    ["docker", "rm", container_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

            # 3. Create ext4 filesystem and populate it
            if self._loopfs_helper_path() is not None:
                self._create_ext4_with_loopfs(tar_path, rootfs_path, rootfs_size_mb, tmp_path)
            else:
                self._create_ext4_with_docker(tar_path, rootfs_path, rootfs_size_mb, tmp_path)

            # 4. Download architecture-compatible kernel
            resolved_kernel_url = kernel_url or self._kernel_url_for_host()
            logger.info(
                "  [4/4] Downloading kernel for host arch from %s",
                resolved_kernel_url,
            )
            self._download_kernel(resolved_kernel_url, kernel_path)

            # 5. Write cache fingerprint if provided and successful
            if fingerprint_data is not None:
                self._write_fingerprint(image_dir, fingerprint_data)