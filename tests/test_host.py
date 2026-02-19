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

"""Tests for SmolVM host module."""

import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import HostError
from smolvm.host import (
    DEFAULT_FIRECRACKER_VERSION,
    HostCapability,
    HostInfo,
    HostManager,
)


@pytest.fixture
def host_manager() -> HostManager:
    """Create a HostManager instance."""
    return HostManager()


class TestDetectArch:
    """Tests for architecture detection."""

    @patch("smolvm.host.platform.machine", return_value="x86_64")
    def test_detect_x86_64(self, mock_machine: MagicMock, host_manager: HostManager) -> None:
        """Test detecting x86_64 architecture."""
        assert host_manager.detect_arch() == "x86_64"

    @patch("smolvm.host.platform.machine", return_value="aarch64")
    def test_detect_aarch64(self, mock_machine: MagicMock, host_manager: HostManager) -> None:
        """Test detecting aarch64 architecture."""
        assert host_manager.detect_arch() == "aarch64"


class TestCheckKVM:
    """Tests for KVM availability checks."""

    @patch("smolvm.host.os.access", return_value=True)
    @patch("smolvm.host.Path.exists", return_value=True)
    def test_kvm_available(
        self, mock_exists: MagicMock, mock_access: MagicMock, host_manager: HostManager
    ) -> None:
        """Test KVM is detected when /dev/kvm exists with permissions."""
        assert host_manager.check_kvm() is True

    @patch("smolvm.host.Path.exists", return_value=False)
    def test_kvm_missing(self, mock_exists: MagicMock, host_manager: HostManager) -> None:
        """Test KVM not detected when /dev/kvm doesn't exist."""
        assert host_manager.check_kvm() is False

    @patch("smolvm.host.os.access", return_value=False)
    @patch("smolvm.host.Path.exists", return_value=True)
    def test_kvm_no_permissions(
        self, mock_exists: MagicMock, mock_access: MagicMock, host_manager: HostManager
    ) -> None:
        """Test KVM detected but no R/W permissions."""
        assert host_manager.check_kvm() is False


class TestCheckDependencies:
    """Tests for dependency checking."""

    @patch("smolvm.host.which")
    def test_all_present(self, mock_which: MagicMock, host_manager: HostManager) -> None:
        """Test when all dependencies are present."""
        mock_which.return_value = Path("/usr/bin/something")

        missing = host_manager.check_dependencies()

        assert missing == []

    @patch("smolvm.host.which")
    def test_some_missing(self, mock_which: MagicMock, host_manager: HostManager) -> None:
        """Test when some dependencies are missing."""

        def side_effect(binary: str) -> Path | None:
            return Path(f"/usr/bin/{binary}") if binary != "nft" else None

        mock_which.side_effect = side_effect

        missing = host_manager.check_dependencies()

        assert len(missing) == 1
        assert "nft" in missing[0]


class TestFindFirecracker:
    """Tests for finding the Firecracker binary."""

    @patch("smolvm.host.which", return_value=Path("/usr/local/bin/firecracker"))
    def test_found_in_path(self, mock_which: MagicMock, host_manager: HostManager) -> None:
        """Test finding firecracker in system PATH."""
        result = host_manager.find_firecracker()

        assert result == Path("/usr/local/bin/firecracker")

    @patch("smolvm.host.os.access", return_value=True)
    @patch("smolvm.host.which", return_value=None)
    def test_found_in_smolvm_dir(
        self,
        mock_which: MagicMock,
        mock_access: MagicMock,
        host_manager: HostManager,
        tmp_path: Path,
    ) -> None:
        """Test finding firecracker in ~/.smolvm/bin/."""
        # Override BIN_DIR to use tmp_path
        host_manager.BIN_DIR = tmp_path / "bin"
        host_manager.BIN_DIR.mkdir(parents=True)
        fc_binary = host_manager.BIN_DIR / "firecracker"
        fc_binary.touch()

        result = host_manager.find_firecracker()

        assert result == fc_binary

    @patch("smolvm.host.which", return_value=None)
    def test_not_found(self, mock_which: MagicMock, host_manager: HostManager) -> None:
        """Test when firecracker is not found anywhere."""
        # BIN_DIR default points to ~/.smolvm/bin which doesn't have it
        host_manager.BIN_DIR = Path("/nonexistent/dir")

        result = host_manager.find_firecracker()

        assert result is None


class TestInstallFirecracker:
    """Tests for Firecracker installation."""

    @patch("smolvm.host.platform.machine", return_value="armv7l")
    def test_unsupported_arch_raises(
        self, mock_machine: MagicMock, host_manager: HostManager
    ) -> None:
        """Test that unsupported architecture raises HostError."""
        with pytest.raises(HostError, match="Unsupported architecture"):
            host_manager.install_firecracker()

    @patch("smolvm.host.requests.get")
    @patch("smolvm.host.platform.machine", return_value="x86_64")
    def test_install_success(
        self,
        mock_machine: MagicMock,
        mock_get: MagicMock,
        host_manager: HostManager,
        tmp_path: Path,
    ) -> None:
        """Test successful firecracker installation."""
        # Override paths
        host_manager.BIN_DIR = tmp_path / "bin"

        # Create a fake tarball with a firecracker binary inside
        version = DEFAULT_FIRECRACKER_VERSION
        arch = "x86_64"
        tarball_path = tmp_path / "fc.tgz"
        inner_dir = f"release-{version}-{arch}"
        binary_name = f"firecracker-{version}-{arch}"

        inner_path = tmp_path / inner_dir
        inner_path.mkdir()
        fake_binary = inner_path / binary_name
        fake_binary.write_text("#!/bin/sh\necho firecracker")

        with tarfile.open(tarball_path, "w:gz") as tar:
            tar.add(inner_path, arcname=inner_dir)

        # Mock the HTTP response to stream the tarball
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.iter_content = lambda chunk_size: iter([tarball_path.read_bytes()])
        mock_get.return_value = mock_response

        result = host_manager.install_firecracker()

        assert result.exists()
        assert result.name == "firecracker"


class TestValidate:
    """Tests for full host validation."""

    @patch("smolvm.host.which")
    @patch("smolvm.host.os.access", return_value=True)
    @patch("smolvm.host.Path.exists", return_value=True)
    @patch("smolvm.host.platform.machine", return_value="x86_64")
    def test_validate_all_good(
        self,
        mock_machine: MagicMock,
        mock_exists: MagicMock,
        mock_access: MagicMock,
        mock_which: MagicMock,
        host_manager: HostManager,
    ) -> None:
        """Test validation when everything is available."""
        mock_which.return_value = Path("/usr/bin/something")

        info = host_manager.validate()

        assert isinstance(info, HostInfo)
        assert info.arch == "x86_64"
        assert info.capabilities[HostCapability.KVM] is True
        assert info.missing_deps == []

    @patch("smolvm.host.which", return_value=None)
    @patch("smolvm.host.Path.exists", return_value=False)
    @patch("smolvm.host.platform.machine", return_value="x86_64")
    def test_validate_missing_everything(
        self,
        mock_machine: MagicMock,
        mock_exists: MagicMock,
        mock_which: MagicMock,
        host_manager: HostManager,
    ) -> None:
        """Test validation when nothing is available."""
        host_manager.BIN_DIR = Path("/nonexistent")

        info = host_manager.validate()

        assert info.capabilities[HostCapability.KVM] is False
        assert info.capabilities[HostCapability.FIRECRACKER] is False
        assert len(info.missing_deps) > 0


class TestHostManagerInit:
    """Tests for HostManager initialization."""

    def test_default_version(self) -> None:
        """Test default Firecracker version is set."""
        hm = HostManager()
        assert hm.firecracker_version == DEFAULT_FIRECRACKER_VERSION

    def test_custom_version(self) -> None:
        """Test custom Firecracker version."""
        hm = HostManager(firecracker_version="v1.13.0")
        assert hm.firecracker_version == "v1.13.0"

    def test_empty_version_raises(self) -> None:
        """Test that empty version raises ValueError."""
        with pytest.raises(ValueError, match="firecracker_version cannot be empty"):
            HostManager(firecracker_version="")
