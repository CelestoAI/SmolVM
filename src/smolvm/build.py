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
from smolvm.utils import RUNTIME_PRIVILEGE_SETUP_HINT, run_command

logger = logging.getLogger(__name__)

# Default boot args that include init=/init for our custom init script
SSH_BOOT_ARGS = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/init"

# Boot args for OpenClaw VMs — 8250.nr_uarts=0 disables serial UART to avoid
# vCPU exits on /dev/ttyS0 writes, which become a measurable host CPU tax at
# 200+ VMs.  No console= since we don't need serial output in production.
OPENCLAW_BOOT_ARGS = "reboot=k panic=1 pci=off init=/init 8250.nr_uarts=0"

# Firecracker-compatible uncompressed kernels.
FIRECRACKER_KERNEL_URLS = {
    "x86_64": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.6/x86_64/vmlinux-5.10.198",
    "aarch64": "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.6/aarch64/vmlinux-5.10.198",
}

# QEMU-compatible kernels (Ubuntu cloud kernels, unpacked).
QEMU_KERNEL_URLS = {
    "x86_64": (
        "https://cloud-images.ubuntu.com/jammy/current/unpacked/"
        "jammy-server-cloudimg-amd64-vmlinuz-generic"
    ),
    "aarch64": (
        "https://cloud-images.ubuntu.com/jammy/current/unpacked/"
        "jammy-server-cloudimg-arm64-vmlinuz-generic"
    ),
}

LOOPFS_HELPER_PATH = Path("/usr/local/libexec/smolvm-loopfs-helper")


class ImageBuilder:
    """Builds custom VM images with SSH pre-configured.

    Example usage::

        from smolvm import ImageBuilder, SmolVM, VMConfig
        from smolvm.build import SSH_BOOT_ARGS

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
        """Check if Docker is available."""
        try:
            subprocess.run(
                ["docker", "version"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def build_alpine_ssh(
        self,
        name: str = "alpine-ssh",
        ssh_password: str = "smolvm",
        rootfs_size_mb: int = 512,
        kernel_url: str | None = None,
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
        if not self.check_docker():
            raise ImageError(
                "Docker is required to build images. "
                "Install Docker Desktop (macOS) or docker.io (Linux)."
            )

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        # Check fingerprint cache
        fingerprint_data = {
            "rootfs_size_mb": rootfs_size_mb,
            "kernel_url": kernel_url,
            "ssh_password": ssh_password,
        }
        if (
            kernel_path.exists()
            and rootfs_path.exists()
            and self._check_fingerprint(image_dir, fingerprint_data)
        ):
            logger.info("Image '%s' already exists and fingerprint matches at %s", name, image_dir)
            return (kernel_path, rootfs_path)

        logger.info("Building Alpine SSH image '%s'...", name)
        image_dir.mkdir(parents=True, exist_ok=True)

        # The /init script runs as PID 1 inside the VM and brings up SSH.
        init_script = self._default_init_script()

        dockerfile_content = """
FROM alpine:3.19

ARG SSH_PASSWORD

# Install SSH and networking utilities
RUN apk add --no-cache \\
    openssh \\
    iproute2 \\
    curl \\
    bash

# Configure SSH
RUN ssh-keygen -A && \\
    sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config && \\
    echo "root:${SSH_PASSWORD}" | chpasswd

# Install our custom init script
COPY init /init
RUN chmod +x /init
"""

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
                kernel_url=kernel_url,
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
        if not self.check_docker():
            raise ImageError(
                "Docker is required to build images. "
                "Install Docker Desktop (macOS) or docker.io (Linux)."
            )

        key_value = self._resolve_public_key(ssh_public_key)

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        fingerprint_data = {
            "rootfs_size_mb": rootfs_size_mb,
            "kernel_url": kernel_url,
            "ssh_public_key": key_value,
        }

        if kernel_path.exists() and rootfs_path.exists():
            if self._check_fingerprint(image_dir, fingerprint_data):
                logger.info(
                    "Image '%s' already exists and fingerprint matches at %s", name, image_dir
                )
                return (kernel_path, rootfs_path)

            logger.info("SSH key or config changed for image '%s'. Rebuilding...", name)
            # Remove stale files
            kernel_path.unlink(missing_ok=True)
            rootfs_path.unlink(missing_ok=True)

        logger.info("Building Alpine key-only SSH image '%s'...", name)
        image_dir.mkdir(parents=True, exist_ok=True)

        init_script = self._default_init_script()

        dockerfile_content = """
FROM alpine:3.19

RUN apk add --no-cache \
    openssh \
    iproute2 \
    curl \
    bash

RUN ssh-keygen -A && \
    mkdir -p /root/.ssh && chmod 700 /root/.ssh && \
    sed -i 's/#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && \
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

COPY authorized_keys /root/.ssh/authorized_keys
RUN chmod 600 /root/.ssh/authorized_keys && chown -R root:root /root/.ssh

COPY init /init
RUN chmod +x /init
"""

        try:
            self._do_build(
                name,
                dockerfile_content,
                init_script,
                image_dir,
                kernel_path,
                rootfs_path,
                rootfs_size_mb,
                extra_files={"authorized_keys": f"{key_value}\n"},
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

    def build_debian_ssh_key(
        self,
        ssh_public_key: str | Path,
        name: str = "debian-ssh-key",
        rootfs_size_mb: int = 2048,
        base_image: str = "debian:bookworm-slim",
        kernel_url: str | None = None,
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
        if not self.check_docker():
            raise ImageError(
                "Docker is required to build images. "
                "Install Docker Desktop (macOS) or docker.io (Linux)."
            )

        key_value = self._resolve_public_key(ssh_public_key)

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        fingerprint_data = {
            "rootfs_size_mb": rootfs_size_mb,
            "kernel_url": kernel_url,
            "ssh_public_key": key_value,
            "base_image": base_image,
        }

        if kernel_path.exists() and rootfs_path.exists():
            if self._check_fingerprint(image_dir, fingerprint_data):
                logger.info(
                    "Image '%s' already exists and fingerprint matches at %s", name, image_dir
                )
                return (kernel_path, rootfs_path)

            logger.info("Inputs changed for image '%s'. Rebuilding...", name)
            # Remove stale files
            kernel_path.unlink(missing_ok=True)
            rootfs_path.unlink(missing_ok=True)

        logger.info("Building Debian key-only SSH image '%s'...", name)
        image_dir.mkdir(parents=True, exist_ok=True)

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
    && rm -rf /var/lib/apt/lists/*

RUN ssh-keygen -A && \\
    mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh && \\
    sed -ri 's/^#?PermitRootLogin .*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PubkeyAuthentication .*/PubkeyAuthentication yes/' /etc/ssh/sshd_config

COPY authorized_keys /root/.ssh/authorized_keys
RUN chmod 600 /root/.ssh/authorized_keys && chown -R root:root /root/.ssh

COPY init /init
RUN chmod +x /init
"""

        try:
            self._do_build(
                name,
                dockerfile_content,
                init_script,
                image_dir,
                kernel_path,
                rootfs_path,
                rootfs_size_mb,
                extra_files={"authorized_keys": f"{key_value}\n"},
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

    def build_openclaw_rootfs(
        self,
        name: str = "openclaw",
        # Note: 'smolvm' is intentionally kept as the default for simplified local
        # demos and testing fixtures. Production usages should override this value.
        ssh_password: str = "smolvm",
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
            ssh_password: Root password for SSH (default: smolvm).
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
            raise ImageError(
                "Docker is required to build images. "
                "Install Docker Desktop (macOS) or docker.io (Linux)."
            )

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

        fingerprint_data = {
            "rootfs_size_mb": rootfs_size_mb,
            "kernel_url": kernel_url,
            "ssh_password": ssh_password,
            "ssh_public_key": key_value,
            "extra_packages": extra_packages,
        }

        if kernel_path.exists() and rootfs_path.exists():
            if self._check_fingerprint(image_dir, fingerprint_data):
                logger.info(
                    "Image '%s' already exists and fingerprint matches at %s", name, image_dir
                )
                return (kernel_path, rootfs_path)

            logger.info("Inputs changed for OpenClaw image '%s'. Rebuilding...", name)
            kernel_path.unlink(missing_ok=True)
            rootfs_path.unlink(missing_ok=True)

        packages_str = " ".join(extra_packages)

        logger.info("Building OpenClaw image '%s' with extra packages: %s...", name, packages_str)
        image_dir.mkdir(parents=True, exist_ok=True)

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

ARG SSH_PASSWORD
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

RUN ssh-keygen -A && \\
    mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh && \\
    sed -ri 's/^#?PermitRootLogin .*/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -ri 's/^#?PasswordAuthentication .*/PasswordAuthentication yes/' /etc/ssh/sshd_config && \\
    echo "root:${{SSH_PASSWORD}}" | chpasswd

# Inject authorized_keys if provided
COPY authorized_keys /root/.ssh/authorized_keys
RUN chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true

# Prepare OpenClaw directories and workspace
RUN useradd -m -s /bin/bash node 2>/dev/null || true && \\
    mkdir -p /opt/openclaw /home/node/.openclaw/devices /workspace && \\
    chown -R node:node /opt/openclaw /home/node/.openclaw /workspace

WORKDIR /opt/openclaw
RUN npm init -y && \\
    npm --prefix /opt/openclaw install -g openclaw && \\
    ln -sf /opt/openclaw/bin/openclaw /usr/local/bin/openclaw && \\
    touch /var/log/openclaw.log && \\
    chown node:node /var/log/openclaw.log

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
                    "authorized_keys": f"{key_value}\n" if key_value else "",
                },
                build_args={"SSH_PASSWORD": ssh_password},
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
mount -t tmpfs tmpfs /tmp

log_ts "mounts-ready"

# Disable Ctrl+Alt+Del hardware reboot — send SIGINT to PID 1 instead
echo 0 > /proc/sys/kernel/ctrl-alt-del

# Remount root read-write
mount -o remount,rw /

# Create required directories
mkdir -p /run/sshd /var/log

log_ts "root-ready"

# ── Networking ───────────────────────────────────────────────
log_ts "net-config-start"
# Configure from kernel command line ip= parameter
# Format: ip=<guest_ip>::<gateway>:<netmask>::eth0:off
IP_CONFIG=$(cat /proc/cmdline | tr ' ' '\n' | grep '^ip=' | head -1)
if [ -n "$IP_CONFIG" ]; then
    GUEST_IP=$(echo "$IP_CONFIG" | cut -d= -f2 | cut -d: -f1)
    GATEWAY=$(echo "$IP_CONFIG" | cut -d= -f2 | cut -d: -f3)
else
    GUEST_IP="172.16.0.2"
    GATEWAY="172.16.0.1"
fi

ip link set lo up
ip link set eth0 up
ip addr add "${{GUEST_IP}}/24" dev eth0 2>/dev/null || true
ip route add default via "${{GATEWAY}}" dev eth0 2>/dev/null || true

# DNS
echo "nameserver 8.8.8.8" > /etc/resolv.conf
echo "nameserver 8.8.4.4" >> /etc/resolv.conf

hostname {custom_hostname}
log_ts "net-ready"

# ── SSH ──────────────────────────────────────────────────────
log_ts "ssh-hostkey-check-start"
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
    echo "SmolVM init: SSH host keys missing; generating..."
    ssh-keygen -A 2>/dev/null
fi
log_ts "ssh-hostkey-check-done"

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
        if arch in {"x86_64", "amd64"}:
            return "x86_64"
        if arch in {"arm64", "aarch64"}:
            return "aarch64"
        raise ImageError(f"Unsupported host architecture '{arch}'")

    def _kernel_url_for_host(self) -> str:
        """Return a Firecracker-compatible kernel URL for the current host arch."""
        arch_key = self._host_arch_key()
        return FIRECRACKER_KERNEL_URLS[arch_key]

    def qemu_kernel_url_for_host(self) -> str:
        """Return a QEMU-compatible kernel URL for the current host arch."""
        arch_key = self._host_arch_key()
        return QEMU_KERNEL_URLS[arch_key]

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
        """Download kernel image to *dest* without external wget dependency."""
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

        rootfs_dir = tmpdir / "rootfs-dir"
        rootfs_dir.mkdir()

        with tarfile.open(tar_path, "r") as tar:
            for member in tar.getmembers():
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ImageError(
                        f"Refusing to extract suspicious tar path from docker export: {member.name}"
                    )

            # Python 3.14 defaults to a restrictive extraction filter that rejects
            # absolute symlink targets commonly present in container rootfs archives.
            # We already validated member names above, so trusted extraction is safe here.
            try:
                tar.extractall(path=rootfs_dir, filter="fully_trusted")
            except TypeError:
                # Python <3.12 does not support the 'filter' argument.
                tar.extractall(path=rootfs_dir)

        rootfs_path.unlink(missing_ok=True)
        rootfs_name = shlex.quote(rootfs_path.name)
        shell_cmd = (
            "set -e; "
            "apk add --no-cache e2fsprogs >/dev/null; "
            f"mke2fs -d /work/rootfs -t ext4 -F /work/out/{rootfs_name} "
            f"{rootfs_size_mb}M >/dev/null"
        )

        try:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{rootfs_dir.resolve()}:/work/rootfs:ro",
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
                f"Command: docker run ... mke2fs\n"
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

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Write Dockerfile and init script
            (tmp_path / "Dockerfile").write_text(dockerfile_content)
            (tmp_path / "init").write_text(init_script)
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
