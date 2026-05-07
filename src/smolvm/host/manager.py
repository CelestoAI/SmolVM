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

"""Host environment management for SmolVM.

Validates the host environment and manages the Firecracker binary.
"""

import logging
import os
import platform
import shutil
import stat
import tarfile
import tempfile
from enum import Enum
from pathlib import Path, PurePosixPath

import requests
from pydantic import BaseModel

from smolvm.exceptions import HostError
from smolvm.utils import which

logger = logging.getLogger(__name__)

# Pinned Firecracker version — bump this to upgrade the default
DEFAULT_FIRECRACKER_VERSION = "v1.14.1"

# GitHub release URL template.
# {version} includes the "v" prefix, {arch} is x86_64 or aarch64.
FIRECRACKER_RELEASE_URL = (
    "https://github.com/firecracker-microvm/firecracker/releases"
    "/download/{version}/firecracker-{version}-{arch}.tgz"
)

SUPPORTED_ARCHITECTURES = {"x86_64", "aarch64"}


class HostCapability(str, Enum):
    """Host capabilities that SmolVM depends on."""

    KVM = "kvm"
    NET_TOOLS = "net_tools"
    FIRECRACKER = "firecracker"


class HostInfo(BaseModel):
    """Summary of host environment validation.

    Attributes:
        arch: CPU architecture (e.g., "x86_64").
        capabilities: Map of capability to availability.
        missing_deps: List of missing dependency names.
        firecracker_path: Path to the Firecracker binary, if found.
    """

    arch: str
    capabilities: dict[HostCapability, bool]
    missing_deps: list[str]
    firecracker_path: Path | None = None

    model_config = {"frozen": True}


class HostManager:
    """Validates host environment and manages the Firecracker binary.

    The default installation directory is ``~/.smolvm/bin/``.
    """

    SMOLVM_HOME = Path.home() / ".smolvm"
    BIN_DIR = SMOLVM_HOME / "bin"

    def __init__(self, firecracker_version: str = DEFAULT_FIRECRACKER_VERSION) -> None:
        """Initialize the host manager.

        Args:
            firecracker_version: Pinned Firecracker version to install
                when auto-installing (e.g., "v1.14.1").
        """
        if not firecracker_version:
            raise ValueError("firecracker_version cannot be empty")

        self.firecracker_version = firecracker_version

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect_arch(self) -> str:
        """Detect the host CPU architecture.

        Returns:
            Architecture string (e.g., "x86_64", "aarch64").
        """
        arch = platform.machine()
        logger.debug("Detected architecture: %s", arch)
        return arch

    def check_kvm(self) -> bool:
        """Check if /dev/kvm exists and is accessible.

        Returns:
            True if KVM is available with R/W permissions.
        """
        kvm_path = Path("/dev/kvm")

        if not kvm_path.exists():
            logger.debug("/dev/kvm does not exist")
            return False

        if not os.access(kvm_path, os.R_OK | os.W_OK):
            logger.debug("/dev/kvm exists but is not readable/writable by current user")
            return False

        logger.debug("/dev/kvm is available with R/W access")
        return True

    def check_dependencies(self) -> list[str]:
        """Check for required system dependencies.

        Returns:
            List of missing dependency names (empty if all present).
        """
        required = {
            "ip": "iproute2",
            "nft": "nftables",
            "ssh": "openssh-client",
        }

        missing: list[str] = []
        for binary, package in required.items():
            if which(binary) is None:
                missing.append(f"'{binary}' not found (install {package})")
                logger.warning("Missing dependency: %s", binary)
            else:
                logger.debug("Found dependency: %s", binary)

        return missing

    def find_firecracker(self) -> Path | None:
        """Find the Firecracker binary.

        Searches:
        1. System PATH
        2. ``~/.smolvm/bin/firecracker``

        Returns:
            Path to the binary, or None if not found.
        """
        # Check system PATH first
        system_path = which("firecracker")
        if system_path is not None:
            logger.debug("Found firecracker in PATH: %s", system_path)
            return system_path

        # Check SmolVM local install
        local_path = self.BIN_DIR / "firecracker"
        if local_path.exists() and os.access(local_path, os.X_OK):
            logger.debug("Found firecracker at: %s", local_path)
            return local_path

        logger.debug("Firecracker binary not found")
        return None

    def install_firecracker(self, version: str | None = None) -> Path:
        """Download and install Firecracker from GitHub releases.

        Downloads the official tarball, extracts the firecracker binary,
        and installs it to ``~/.smolvm/bin/``.

        Args:
            version: Version to install (e.g., "v1.14.1").
                Defaults to the pinned version from ``__init__``.

        Returns:
            Path to the installed binary.

        Raises:
            HostError: If architecture is unsupported, download fails,
                or extraction fails.
        """
        if version is None:
            version = self.firecracker_version

        arch = self.detect_arch()
        if arch not in SUPPORTED_ARCHITECTURES:
            raise HostError(
                f"Unsupported architecture: {arch}. "
                f"Firecracker supports: {', '.join(sorted(SUPPORTED_ARCHITECTURES))}"
            )

        url = FIRECRACKER_RELEASE_URL.format(version=version, arch=arch)
        logger.info("Downloading Firecracker %s for %s from %s", version, arch, url)

        # Ensure install directory exists
        self.BIN_DIR.mkdir(parents=True, exist_ok=True)
        dest = self.BIN_DIR / "firecracker"

        try:
            self._download_and_extract(url, dest, version, arch)
        except requests.RequestException as e:
            raise HostError(f"Failed to download Firecracker: {e}") from e
        except (tarfile.TarError, OSError) as e:
            raise HostError(f"Failed to extract Firecracker: {e}") from e

        # Verify the binary exists and is executable
        if not dest.exists():
            raise HostError(f"Firecracker binary not found after extraction at {dest}")

        logger.info("Firecracker %s installed at: %s", version, dest)
        return dest

    def validate(self) -> HostInfo:
        """Run all host validation checks.

        Returns:
            HostInfo summary of the validation results.
        """
        arch = self.detect_arch()
        kvm_ok = self.check_kvm()
        missing_deps = self.check_dependencies()
        fc_path = self.find_firecracker()

        capabilities = {
            HostCapability.KVM: kvm_ok,
            HostCapability.NET_TOOLS: len(missing_deps) == 0,
            HostCapability.FIRECRACKER: fc_path is not None,
        }

        info = HostInfo(
            arch=arch,
            capabilities=capabilities,
            missing_deps=missing_deps,
            firecracker_path=fc_path,
        )

        logger.info(
            "Host validation: arch=%s, kvm=%s, net_tools=%s, firecracker=%s",
            arch,
            kvm_ok,
            capabilities[HostCapability.NET_TOOLS],
            fc_path,
        )

        return info

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _download_and_extract(self, url: str, dest: Path, version: str, arch: str) -> None:
        """Download a tarball and extract the firecracker binary.

        Args:
            url: URL of the tarball.
            dest: Destination path for the extracted binary.
            version: Firecracker version string.
            arch: Architecture string.

        Raises:
            requests.RequestException: On download failure.
            tarfile.TarError: On extraction failure.
            HostError: If the expected binary is not in the archive.
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            tarball_path = Path(tmp_dir) / "firecracker.tgz"

            # Stream download
            response = requests.get(url, stream=True, timeout=120)
            response.raise_for_status()

            with open(tarball_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            logger.debug("Downloaded tarball to %s", tarball_path)

            # Extract
            with tarfile.open(tarball_path, "r:gz") as tar:
                # Security: validate member paths to prevent path traversal
                for member in tar.getmembers():
                    if member.name.startswith("/") or ".." in PurePosixPath(member.name).parts:
                        raise HostError(f"Refusing to extract suspicious path: {member.name}")
                if hasattr(tarfile, "data_filter"):
                    tar.extractall(path=tmp_dir, filter="data")
                else:
                    # Python < 3.12 without PEP 706 backport — fall back to
                    # unfiltered extraction.  The path validation above
                    # guards against absolute/.. paths.
                    tar.extractall(path=tmp_dir)  # noqa: S202

            # Find the firecracker binary in extracted contents
            # Tarball structure: release-{version}-{arch}/firecracker-{version}-{arch}
            expected_name = f"firecracker-{version}-{arch}"
            binary_path = self._find_binary_in_dir(Path(tmp_dir), expected_name)

            if binary_path is None:
                raise HostError(f"Could not find '{expected_name}' in the downloaded archive")

            # Make executable and move to destination
            binary_path.chmod(binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

            # Atomic-ish move: copy to tmp in dest dir, then rename
            tmp_dest = dest.with_suffix(".tmp")
            try:
                shutil.copy2(binary_path, tmp_dest)
                tmp_dest.rename(dest)
            except (OSError, shutil.Error):
                tmp_dest.unlink(missing_ok=True)
                raise

    @staticmethod
    def _find_binary_in_dir(directory: Path, name: str) -> Path | None:
        """Recursively find a binary by name in a directory.

        Args:
            directory: Directory to search.
            name: Binary filename to find.

        Returns:
            Path to the binary, or None if not found.
        """
        for path in directory.rglob(name):
            if path.is_file():
                return path
        return None
