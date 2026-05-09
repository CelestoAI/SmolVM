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

"""Tests for tar extraction security (PR #290 follow-up)."""

import io
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import HostError


# ---------------------------------------------------------------------------
# The dashboard server module has heavy top-level imports (fastapi, uvicorn,
# websockets …) that live in the optional ``dashboard`` extra.  The CI test
# matrix only installs the ``dev`` extra, so those packages are absent.
# ``_extract_dashboard_dist`` is a pure-stdlib helper (tarfile / pathlib)
# that does not need any of them.  We inject lightweight stubs into
# ``sys.modules`` so the module can be imported without pulling in the real
# packages.
# ---------------------------------------------------------------------------
_DASHBOARD_STUB_MODULES: list[str] = [
    "fastapi",
    "fastapi.middleware",
    "fastapi.middleware.cors",
    "fastapi.responses",
    "fastapi.staticfiles",
    "uvicorn",
    "websockets",
    "smolvm.dashboard.commands",
    "smolvm.dashboard.connection_manager",
    "smolvm.dashboard.poller",
]


def _ensure_dashboard_importable() -> None:  # pragma: no cover
    """Insert stubs for optional dashboard dependencies if missing."""
    for mod_name in _DASHBOARD_STUB_MODULES:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()


def _make_tarball_with_member(arcname: str) -> bytes:
    """Create a minimal .tar.gz containing a single file at *arcname*."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=arcname)
        data = b"hello"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()

def _mock_response(tarball_bytes: bytes) -> MagicMock:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.iter_content = lambda chunk_size: iter([tarball_bytes])
    return mock_response

def _make_spying_tar_open(
    original_open, extractall_calls: list[dict]
):
    """Return a ``tarfile.open`` replacement that records *extractall* kwargs.

    Using a factory keeps the spy class in one place so every test shares the
    same implementation.
    """

    class _SpyingTarFile:
        """Wraps a real TarFile to record extractall calls."""

        def __init__(self, real_tar: tarfile.TarFile) -> None:
            self._tar = real_tar

        def getmembers(self):
            return self._tar.getmembers()

        def extractall(self, **kwargs):
            extractall_calls.append(kwargs)
            return self._tar.extractall(**kwargs)  # noqa: S202

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._tar.__exit__(*args)

    def _spying_open(*args, **kwargs):
        return _SpyingTarFile(original_open(*args, **kwargs))

    return _spying_open

class TestHostManagerTarExtraction:
    """Verify path-traversal guards in HostManager._download_and_extract."""

    def test_rejects_dotdot_in_member_name(self, tmp_path: Path) -> None:
        """Member names containing '..' must be rejected."""
        from smolvm.host.manager import HostManager

        tarball_bytes = _make_tarball_with_member("foo/../../etc/passwd")
        mock_get = patch(
            "smolvm.host.manager.requests.get",
            return_value=_mock_response(tarball_bytes),
        )

        hm = HostManager()
        dest = tmp_path / "fc"
        with mock_get, pytest.raises(HostError, match="suspicious path"):
            hm._download_and_extract(
                url="http://example.com/fc.tgz", dest=dest,
                version="v1.13.0", arch="x86_64",
            )

    def test_rejects_absolute_member_name(self, tmp_path: Path) -> None:
        """Member names starting with '/' must be rejected."""
        from smolvm.host.manager import HostManager

        tarball_bytes = _make_tarball_with_member("/etc/passwd")
        mock_get = patch(
            "smolvm.host.manager.requests.get",
            return_value=_mock_response(tarball_bytes),
        )

        hm = HostManager()
        dest = tmp_path / "fc"
        with mock_get, pytest.raises(HostError, match="suspicious path"):
            hm._download_and_extract(
                url="http://example.com/fc.tgz", dest=dest,
                version="v1.13.0", arch="x86_64",
            )

    def test_rejects_dotdot_at_start(self, tmp_path: Path) -> None:
        """Member name starting with '..' (no slash prefix) must be rejected."""
        from smolvm.host.manager import HostManager

        tarball_bytes = _make_tarball_with_member("../../etc/shadow")
        mock_get = patch(
            "smolvm.host.manager.requests.get",
            return_value=_mock_response(tarball_bytes),
        )

        hm = HostManager()
        dest = tmp_path / "fc"
        with mock_get, pytest.raises(HostError, match="suspicious path"):
            hm._download_and_extract(
                url="http://example.com/fc.tgz", dest=dest,
                version="v1.13.0", arch="x86_64",
            )

    def test_accepts_valid_member_name(self, tmp_path: Path) -> None:
        """Legitimate member names should extract without error."""
        from smolvm.host.manager import HostManager

        version = "v1.13.0"
        arch = "x86_64"
        inner_dir = f"release-{version}-{arch}"
        binary_name = f"firecracker-{version}-{arch}"

        # Build a real tarball with a binary inside
        inner_path = tmp_path / inner_dir
        inner_path.mkdir()
        fake_binary = inner_path / binary_name
        fake_binary.write_text("#!/bin/sh\necho firecracker")

        tarball_path = tmp_path / "fc.tgz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            tar.add(inner_path, arcname=inner_dir)

        mock_get = patch(
            "smolvm.host.manager.requests.get",
            return_value=_mock_response(tarball_path.read_bytes()),
        )

        dest = tmp_path / "bin" / "firecracker"
        dest.parent.mkdir(parents=True, exist_ok=True)
        hm = HostManager()
        with mock_get:
            hm._download_and_extract(
                url="http://example.com/fc.tgz", dest=dest,
                version=version, arch=arch,
            )

        assert dest.exists()

    def test_uses_data_filter_when_available(self, tmp_path: Path) -> None:
        """When tarfile.data_filter exists, extractall(filter='data') is called."""
        from smolvm.host.manager import HostManager

        version = "v1.13.0"
        arch = "x86_64"
        inner_dir = f"release-{version}-{arch}"
        binary_name = f"firecracker-{version}-{arch}"

        inner_path = tmp_path / inner_dir
        inner_path.mkdir()
        fake_binary = inner_path / binary_name
        fake_binary.write_text("#!/bin/sh\necho firecracker")

        tarball_path = tmp_path / "fc.tgz"
        with tarfile.open(tarball_path, "w:gz") as tar:
            tar.add(inner_path, arcname=inner_dir)

        mock_get = patch(
            "smolvm.host.manager.requests.get",
            return_value=_mock_response(tarball_path.read_bytes()),
        )

        dest = tmp_path / "bin" / "firecracker"
        dest.parent.mkdir(parents=True, exist_ok=True)
        hm = HostManager()

        extractall_calls: list[dict] = []
        spying_open = _make_spying_tar_open(tarfile.open, extractall_calls)

        with mock_get, patch("smolvm.host.manager.tarfile.open", side_effect=spying_open):
            hm._download_and_extract(
                url="http://example.com/fc.tgz", dest=dest,
                version=version, arch=arch,
            )

        assert len(extractall_calls) == 1
        if hasattr(tarfile, "data_filter"):
            assert extractall_calls[0].get("filter") == "data"
        else:
            assert "filter" not in extractall_calls[0]

class TestDashboardExtractDist:
    """Verify path-traversal guards in _extract_dashboard_dist."""

    @pytest.fixture(autouse=True)
    def _stub_optional_deps(self) -> None:
        _ensure_dashboard_importable()

    def test_rejects_dotdot_in_member(self, tmp_path: Path) -> None:
        """Member names containing '..' path parts must be rejected."""
        from smolvm.dashboard.server import _extract_dashboard_dist

        tarball_bytes = _make_tarball_with_member("dist/../../../etc/passwd")
        archive = tmp_path / "archive.tar.gz"
        archive.write_bytes(tarball_bytes)

        with pytest.raises(RuntimeError, match="Unsafe path"):
            _extract_dashboard_dist(archive, tmp_path / "extract")

    def test_rejects_absolute_member(self, tmp_path: Path) -> None:
        """Member names starting with '/' must be rejected."""
        from smolvm.dashboard.server import _extract_dashboard_dist

        tarball_bytes = _make_tarball_with_member("/etc/passwd")
        archive = tmp_path / "archive.tar.gz"
        archive.write_bytes(tarball_bytes)

        with pytest.raises(RuntimeError, match="Unsafe path"):
            _extract_dashboard_dist(archive, tmp_path / "extract")

    def test_extracts_valid_archive(self, tmp_path: Path) -> None:
        """A clean archive with dist/index.html extracts and returns dist dir."""
        from smolvm.dashboard.server import _extract_dashboard_dist

        # Create a real tarball with dist/index.html
        content = tmp_path / "staging"
        content.mkdir()
        dist = content / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html></html>")

        archive = tmp_path / "archive.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(content, arcname=".")

        extract_dir = tmp_path / "extract"
        result = _extract_dashboard_dist(archive, extract_dir)

        assert result.is_dir()
        assert (result / "index.html").is_file()

    def test_uses_data_filter_guard(self, tmp_path: Path) -> None:
        """_extract_dashboard_dist uses hasattr guard for data_filter."""
        from smolvm.dashboard.server import _extract_dashboard_dist

        content = tmp_path / "staging"
        content.mkdir()
        dist = content / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html></html>")

        archive = tmp_path / "archive.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(content, arcname=".")

        extractall_calls: list[dict] = []
        spying_open = _make_spying_tar_open(tarfile.open, extractall_calls)

        with patch("smolvm.dashboard.server.tarfile.open", side_effect=spying_open):
            result = _extract_dashboard_dist(archive, tmp_path / "extract")

        assert result.is_dir()
        assert len(extractall_calls) == 1
        if hasattr(tarfile, "data_filter"):
            assert extractall_calls[0].get("filter") == "data"
        else:
            assert "filter" not in extractall_calls[0]
