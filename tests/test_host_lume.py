# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

import io
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from smolvm.exceptions import HostError
from smolvm.host import lume


def _archive(path: Path, extra_member: str | None = None) -> None:
    files = {
        "lume": b"#!/bin/sh\necho 0.4.0\n",
        "lume.app/Contents/MacOS/lume": b"app-binary",
    }
    if extra_member is not None:
        files[extra_member] = b"unsafe"
    with tarfile.open(path, "w:gz") as bundle:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755
            bundle.addfile(info, io.BytesIO(payload))


def test_verify_lume_app_requires_virtualization_entitlement(tmp_path: Path) -> None:
    app = tmp_path / "lume.app"
    signed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
    entitled = subprocess.CompletedProcess(
        [], 0, stdout="", stderr="<key>com.apple.security.virtualization</key>"
    )
    with patch("smolvm.host.lume.subprocess.run", side_effect=[signed, signed, entitled]):
        lume._verify_lume_app(app)


def test_macos_major_version_falls_back_for_invalid_values() -> None:
    assert lume.macos_major_version("14.6") == 14
    assert lume.macos_major_version("") == 0
    assert lume.macos_major_version("unknown") == 0


def test_install_requires_macos_14_or_newer(tmp_path: Path) -> None:
    with (
        patch("smolvm.host.lume.platform.system", return_value="Darwin"),
        patch("smolvm.host.lume.platform.machine", return_value="arm64"),
        patch("smolvm.host.lume.platform.mac_ver", return_value=("13.6", ("", "", ""), "")),
        pytest.raises(HostError, match="macOS 14 or newer"),
    ):
        lume.install_pinned_lume(destination=tmp_path / "lume")


def test_extract_lume_binary_accepts_one_regular_binary(tmp_path: Path) -> None:
    archive = tmp_path / "lume.tar.gz"
    destination = tmp_path / "lume"
    _archive(archive)

    lume._extract_lume_bundle(archive, destination)

    wrapper = destination / "lume"
    app_binary = destination / "lume.app" / "Contents" / "MacOS" / "lume"
    assert wrapper.read_bytes().startswith(b"#!/bin/sh")
    assert app_binary.read_bytes() == b"app-binary"
    assert wrapper.stat().st_mode & 0o111


def test_extract_lume_binary_rejects_unsafe_member(tmp_path: Path) -> None:
    archive = tmp_path / "lume.tar.gz"
    destination = tmp_path / "lume"
    _archive(archive, "../escape")

    with pytest.raises(HostError, match="unsafe path"):
        lume._extract_lume_bundle(archive, destination)


def test_pinned_lume_ready_requires_exact_version(tmp_path: Path) -> None:
    binary = tmp_path / "lume"
    binary.write_text("#!/bin/sh\necho 0.4.0\n")
    binary.chmod(0o755)

    with patch("smolvm.host.lume.find_lume_binary", return_value=binary):
        assert lume.pinned_lume_ready() is True

    binary.write_text("#!/bin/sh\necho 0.3.0\n")
    with patch("smolvm.host.lume.find_lume_binary", return_value=binary):
        assert lume.pinned_lume_ready() is False
