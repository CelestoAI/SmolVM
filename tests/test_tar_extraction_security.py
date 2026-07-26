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


def _make_tarball_with_symlink(arcname: str, target: str) -> bytes:
    """Create a minimal .tar.gz whose single member is a symlink."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=arcname)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        tar.addfile(info)
    return buf.getvalue()


def _mock_response(tarball_bytes: bytes) -> MagicMock:
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.iter_content = lambda chunk_size: iter([tarball_bytes])
    return mock_response


def _make_spying_tar_open(original_open, extractall_calls: list[dict]):
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
                url="http://example.com/fc.tgz",
                dest=dest,
                version="v1.13.0",
                arch="x86_64",
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
                url="http://example.com/fc.tgz",
                dest=dest,
                version="v1.13.0",
                arch="x86_64",
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
                url="http://example.com/fc.tgz",
                dest=dest,
                version="v1.13.0",
                arch="x86_64",
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
                url="http://example.com/fc.tgz",
                dest=dest,
                version=version,
                arch=arch,
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
                url="http://example.com/fc.tgz",
                dest=dest,
                version=version,
                arch=arch,
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


class TestGuestTarModeBits:
    """The sandbox is untrusted; its tar must not carry mode bits to the host."""

    @staticmethod
    def _guest_tarball() -> bytes:
        """A tar as an untrusted guest agent could produce it."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            directory = tarfile.TarInfo("subdir")
            directory.type = tarfile.DIRTYPE
            directory.mode = 0o2777
            archive.addfile(directory)
            for name, mode in (
                ("setuid_root", 0o4755),
                ("setgid", 0o2755),
                ("sticky", 0o1777),
                ("plain", 0o644),
                ("script", 0o755),
            ):
                member = tarfile.TarInfo(name)
                member.size = 4
                member.mode = mode
                archive.addfile(member, io.BytesIO(b"data"))
        return buffer.getvalue()

    def test_setuid_setgid_and_sticky_bits_are_stripped(self, tmp_path: Path) -> None:
        """A guest tar must not be able to plant a setuid file on the host.

        ``stat.S_IMODE`` keeps 0o7777 — setuid, setgid and sticky included —
        and this archive is produced inside the sandbox and unpacked on the
        host, often under ``sudo`` because SmolVM needs root for host
        networking. Honouring those bits turned a directory download into a
        privilege-escalation primitive.
        """
        import stat as stat_module

        from smolvm.comm.rust_http_vsock_channel import _safe_extract_tar

        destination = tmp_path / "downloaded"
        _safe_extract_tar(self._guest_tarball(), destination)

        dangerous = stat_module.S_ISUID | stat_module.S_ISGID | stat_module.S_ISVTX
        offenders = [path.name for path in destination.iterdir() if path.stat().st_mode & dangerous]
        assert not offenders, f"guest-supplied special mode bits survived on {offenders}"

    def test_ordinary_permissions_are_still_preserved(self, tmp_path: Path) -> None:
        """Stripping the dangerous bits must not flatten normal permissions."""
        import stat as stat_module

        from smolvm.comm.rust_http_vsock_channel import _safe_extract_tar

        destination = tmp_path / "downloaded"
        _safe_extract_tar(self._guest_tarball(), destination)

        modes = {
            path.name: stat_module.S_IMODE(path.stat().st_mode) for path in destination.iterdir()
        }
        assert modes["plain"] == 0o644
        assert modes["script"] == 0o755
        # The dangerous bit is dropped; the permission bits beneath it stay.
        assert modes["setuid_root"] == 0o755
        assert modes["setgid"] == 0o755
        assert modes["sticky"] == 0o777


class TestLinkMembersRejected:
    """Path validation must also cover links, not just ``..`` and ``/``."""

    def test_host_manager_rejects_symlink_member(self, tmp_path: Path) -> None:
        """A symlink escaping the extraction directory must be refused.

        On Python without PEP 706's ``data`` filter these archives are
        extracted unfiltered, and the name check alone does not stop a member
        like ``link -> /etc`` — a later member written "through" that link
        lands outside the temporary directory entirely.
        """
        from smolvm.host.manager import HostManager

        tarball_bytes = _make_tarball_with_symlink("release/link", "/etc")
        mock_get = patch(
            "smolvm.host.manager.requests.get",
            return_value=_mock_response(tarball_bytes),
        )

        hm = HostManager()
        with mock_get, pytest.raises(HostError, match="link or device"):
            hm._download_and_extract(
                url="http://example.com/fc.tgz",
                dest=tmp_path / "fc",
                version="v1.13.0",
                arch="x86_64",
            )

    def test_dashboard_rejects_symlink_member(self, tmp_path: Path) -> None:
        """Same guard on the dashboard archive."""
        _ensure_dashboard_importable()
        from smolvm.dashboard.server import _extract_dashboard_dist

        archive = tmp_path / "dash.tar.gz"
        archive.write_bytes(_make_tarball_with_symlink("dist/link", "/etc"))

        with pytest.raises(RuntimeError, match="Unsafe entry"):
            _extract_dashboard_dist(archive, tmp_path / "out")


class TestGuestFileModeHeader:
    """The guest's file-mode header is untrusted input applied on the host."""

    def test_setuid_and_setgid_are_masked_off(self) -> None:
        """``x-smolvm-file-mode`` must not be able to set setuid on the host.

        This is the single-file sibling of the tar extraction path — the same
        bug, one function away, and it would have survived a fix that only
        looked at directory downloads.
        """
        from smolvm.comm.rust_http_vsock_channel import _parse_mode_header

        assert _parse_mode_header("0o4755") == 0o755
        assert _parse_mode_header("2755") == 0o755
        assert _parse_mode_header("1777") == 0o777

    def test_ordinary_modes_survive(self) -> None:
        """Masking must not disturb normal permissions."""
        from smolvm.comm.rust_http_vsock_channel import _parse_mode_header

        assert _parse_mode_header("644") == 0o644
        assert _parse_mode_header("0o600") == 0o600

    @pytest.mark.parametrize("value", ["not-octal", "-1", ""])
    def test_malformed_mode_raises_a_smolvm_error(self, value: str) -> None:
        """A malformed header is a protocol error, not a bare ValueError."""
        from smolvm.comm.rust_http_vsock_channel import _parse_mode_header
        from smolvm.exceptions import SmolVMError

        with pytest.raises(SmolVMError, match="file mode"):
            _parse_mode_header(value)
