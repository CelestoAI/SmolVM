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

import logging
import subprocess
import tempfile
from pathlib import Path

from smolvm.exceptions import ImageError, SmolVMError
from smolvm.utils import RUNTIME_PRIVILEGE_SETUP_HINT, run_command

logger = logging.getLogger(__name__)

# Default boot args that include init=/init for our custom init script
SSH_BOOT_ARGS = (
    "console=ttyS0 reboot=k panic=1 pci=off init=/init"
)

# Firecracker-compatible uncompressed kernel
FIRECRACKER_KERNEL_URL = (
    "https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.6/x86_64/vmlinux-5.10.198"
)
LOOPFS_HELPER_PATH = Path("/usr/local/libexec/smolvm-loopfs-helper")


class ImageBuilder:
    """Builds custom VM images with SSH pre-configured.

    Example usage::

        from smolvm import ImageBuilder, VM, VMConfig
        from smolvm.build import SSH_BOOT_ARGS

        builder = ImageBuilder()
        kernel, rootfs = builder.build_alpine_ssh()

        config = VMConfig(
            vm_id="my-vm",
            kernel_path=kernel,
            rootfs_path=rootfs,
            boot_args=SSH_BOOT_ARGS,
        )
        with VM(config) as vm:
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

        Returns:
            Tuple of (kernel_path, rootfs_path).

        Raises:
            ImageError: If Docker is not available or build fails.
        """
        if not self.check_docker():
            raise ImageError(
                "Docker is required to build images. "
                "Install with: sudo apt install docker.io"
            )

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        # Return cached image if it exists
        if kernel_path.exists() and rootfs_path.exists():
            logger.info("Image '%s' already exists at %s", name, image_dir)
            return (kernel_path, rootfs_path)

        logger.info("Building Alpine SSH image '%s'...", name)
        image_dir.mkdir(parents=True, exist_ok=True)

        # The /init script runs as PID 1 inside the VM and brings up SSH.
        init_script = self._default_init_script()


        dockerfile_content = f"""
FROM alpine:3.19

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
    echo 'root:{ssh_password}' | chpasswd

# Install our custom init script
COPY init /init
RUN chmod +x /init
"""


        try:
            self._do_build(
                name, dockerfile_content, init_script, image_dir,
                kernel_path, rootfs_path, rootfs_size_mb,
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
    ) -> tuple[Path, Path]:
        """Build Alpine Linux image with key-only SSH access.

        Args:
            ssh_public_key: Public key content or path to a public key file.
            name: Image name for caching.
            rootfs_size_mb: Size of rootfs in MB.

        Returns:
            Tuple of (kernel_path, rootfs_path).
        """
        if not self.check_docker():
            raise ImageError(
                "Docker is required to build images. "
                "Install with: sudo apt install docker.io"
            )

        key_value = self._resolve_public_key(ssh_public_key)

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        if kernel_path.exists() and rootfs_path.exists():
            # Check if the image is stale (older than the provided key file)
            is_stale = False

            # Resolve key path from input if possible
            key_path_check: Path | None = None
            if isinstance(ssh_public_key, Path):
                key_path_check = ssh_public_key
            elif isinstance(ssh_public_key, str):
                try:
                    p = Path(ssh_public_key)
                    if p.exists():
                        key_path_check = p
                except OSError:
                    pass

            # If we found a key file, check its mtime
            if key_path_check and key_path_check.exists():
                try:
                    key_mtime = key_path_check.stat().st_mtime
                    img_mtime = rootfs_path.stat().st_mtime
                    if key_mtime > img_mtime:
                        logger.info(
                            "SSH key '%s' is newer than cached image. Rebuilding...",
                            key_path_check.name
                        )
                        is_stale = True
                except OSError:
                    pass

            if not is_stale:
                logger.info("Image '%s' already exists at %s", name, image_dir)
                return (kernel_path, rootfs_path)

            # Remove stale files
            if kernel_path.exists():
                kernel_path.unlink()
            if rootfs_path.exists():
                rootfs_path.unlink()

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

RUN mkdir -p /root/.ssh && chmod 700 /root/.ssh && \
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
    ) -> tuple[Path, Path]:
        """Build Debian Linux image with key-only SSH access.

        Args:
            ssh_public_key: Public key content or path to a public key file.
            name: Image name for caching.
            rootfs_size_mb: Size of rootfs in MB.
            base_image: Docker base image to build from.

        Returns:
            Tuple of (kernel_path, rootfs_path).
        """
        if not self.check_docker():
            raise ImageError(
                "Docker is required to build images. "
                "Install with: sudo apt install docker.io"
            )

        key_value = self._resolve_public_key(ssh_public_key)

        image_dir = self.cache_dir / name
        kernel_path = image_dir / "vmlinux.bin"
        rootfs_path = image_dir / "rootfs.ext4"

        if kernel_path.exists() and rootfs_path.exists():
            # Check if the image is stale (older than the provided key file)
            is_stale = False

            # Resolve key path from input if possible
            key_path_check: Path | None = None
            if isinstance(ssh_public_key, Path):
                key_path_check = ssh_public_key
            elif isinstance(ssh_public_key, str):
                try:
                    p = Path(ssh_public_key)
                    if p.exists():
                        key_path_check = p
                except OSError:
                    pass

            # If we found a key file, check its mtime
            if key_path_check and key_path_check.exists():
                try:
                    key_mtime = key_path_check.stat().st_mtime
                    img_mtime = rootfs_path.stat().st_mtime
                    if key_mtime > img_mtime:
                        logger.info(
                            "SSH key '%s' is newer than cached image. Rebuilding...",
                            key_path_check.name,
                        )
                        is_stale = True
                except OSError:
                    pass

            if not is_stale:
                logger.info("Image '%s' already exists at %s", name, image_dir)
                return (kernel_path, rootfs_path)

            # Remove stale files
            if kernel_path.exists():
                kernel_path.unlink()
            if rootfs_path.exists():
                rootfs_path.unlink()

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

RUN mkdir -p /run/sshd /root/.ssh && chmod 700 /root/.ssh && \\
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

    def _default_init_script(self) -> str:
        """Default PID 1 init script used by SSH-capable images."""
        return r"""#!/bin/sh
# SmolVM custom init - runs as PID 1 inside Firecracker VM

# Mount essential filesystems
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev 2>/dev/null  # may already be mounted
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /run
mount -t tmpfs tmpfs /tmp

# Remount root read-write
mount -o remount,rw /

# Create required directories
mkdir -p /run/sshd /var/log

# Configure networking from kernel command line ip= parameter
# Format: ip=<guest_ip>::<gateway>:<netmask>::eth0:off
IP_CONFIG=$(cat /proc/cmdline | tr ' ' '\n' | grep '^ip=' | head -1)
if [ -n "$IP_CONFIG" ]; then
    GUEST_IP=$(echo "$IP_CONFIG" | cut -d= -f2 | cut -d: -f1)
    GATEWAY=$(echo "$IP_CONFIG" | cut -d= -f2 | cut -d: -f3)
else
    GUEST_IP="172.16.0.2"
    GATEWAY="172.16.0.1"
fi

# Bring up networking
ip link set lo up
ip link set eth0 up
# Use CIDR /24 — ip command does not accept dotted netmask
ip addr add "${GUEST_IP}/24" dev eth0 2>/dev/null || true
ip route add default via "${GATEWAY}" dev eth0 2>/dev/null || true

# DNS
echo "nameserver 8.8.8.8" > /etc/resolv.conf
echo "nameserver 8.8.4.4" >> /etc/resolv.conf

# Set hostname
hostname smolvm

# Generate host keys if missing
ssh-keygen -A 2>/dev/null

# Start SSH daemon
/usr/sbin/sshd -e

echo "SmolVM init complete: IP=${GUEST_IP}, SSH listening on port 22"

# Keep PID 1 alive (if PID 1 exits the kernel panics)
while true; do
    sleep 3600
done
"""

    def _loopfs_helper_path(self) -> Path | None:
        """Return installed privileged helper path if available."""
        if LOOPFS_HELPER_PATH.is_file():
            return LOOPFS_HELPER_PATH
        return None

    def _run_loopfs(self, action: str, *args: Path) -> None:
        """Run a privileged loopfs action through the scoped helper."""
        helper = self._loopfs_helper_path()
        if helper is None:
            raise ImageError(
                "Missing loopfs helper for image building.\n"
                f"Expected helper at: {LOOPFS_HELPER_PATH}\n"
                f"{RUNTIME_PRIVILEGE_SETUP_HINT}"
            )

        cmd = [str(helper), action, *(str(arg) for arg in args)]
        try:
            run_command(cmd, use_sudo=True, check=True, capture_output=True)
        except SmolVMError as e:
            raise ImageError(
                "Image build loopfs operation failed.\n"
                f"Command: {' '.join(cmd)}\n"
                f"{RUNTIME_PRIVILEGE_SETUP_HINT}\n"
                f"error: {e}"
            ) from e

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
    ) -> None:
        """Execute the Docker build and image conversion."""
        docker_tag = f"smolvm-{name}"

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Write Dockerfile and init script
            (tmpdir / "Dockerfile").write_text(dockerfile_content)
            (tmpdir / "init").write_text(init_script)
            if extra_files:
                for filename, content in extra_files.items():
                    (tmpdir / filename).write_text(content)

            # 1. Build Docker image
            logger.info("  [1/4] Building Docker image...")
            subprocess.run(
                ["docker", "build", "-t", docker_tag, str(tmpdir)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )

            # 2. Export rootfs from container
            logger.info("  [2/4] Exporting rootfs...")
            container_id = subprocess.run(
                ["docker", "create", docker_tag],
                check=True, capture_output=True, text=True,
            ).stdout.strip()

            try:
                tar_path = tmpdir / "rootfs.tar"
                subprocess.run(
                    ["docker", "export", container_id, "-o", str(tar_path)],
                    check=True,
                )
            finally:
                subprocess.run(
                    ["docker", "rm", container_id],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )

            # 3. Create ext4 filesystem and populate it
            logger.info("  [3/4] Creating ext4 filesystem (%dMB)...", rootfs_size_mb)
            subprocess.run(
                ["dd", "if=/dev/zero", f"of={rootfs_path}",
                 "bs=1M", f"count={rootfs_size_mb}"],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["mkfs.ext4", "-F", str(rootfs_path)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            mount_dir = tmpdir / "mnt"
            mount_dir.mkdir()
            self._run_loopfs("mount", rootfs_path, mount_dir)
            tar_error: Exception | None = None
            try:
                self._run_loopfs("extract", tar_path, mount_dir)
            except Exception as e:
                tar_error = e
            finally:
                try:
                    self._run_loopfs("umount", mount_dir)
                except ImageError:
                    if tar_error is None:
                        raise
                    logger.warning(
                        "Failed to unmount rootfs after tar extraction error",
                        exc_info=True,
                    )
            if tar_error is not None:
                raise tar_error

            # 4. Download Firecracker-compatible kernel
            logger.info("  [4/4] Downloading Firecracker kernel...")
            subprocess.run(
                ["wget", "-q", "--show-progress",
                 FIRECRACKER_KERNEL_URL, "-O", str(kernel_path)],
                check=True,
            )
