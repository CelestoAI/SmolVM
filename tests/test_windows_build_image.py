# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Unit tests for the unattended Windows image builder."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.windows import render_autounattend
from smolvm.windows.build_image import (
    _AUTOUNATTEND_VOLUME_LABEL,
    WindowsImageBuilder,
    build_autounattend_iso,
)

# ───────────────────────── render_autounattend ────────────────────────────


def test_render_substitutes_all_four_placeholders() -> None:
    text = render_autounattend(
        username="alice",
        password="secret-pw",
        hostname="winhost-1",
        edition="Windows 11 Home",
    )
    # No template tokens survive.
    assert "@@SMOLVM_" not in text
    # All four values appear at least once.
    assert "<Name>alice</Name>" in text
    assert "<Value>secret-pw</Value>" in text
    assert "<ComputerName>winhost-1</ComputerName>" in text
    assert "<Value>Windows 11 Home</Value>" in text


def test_render_defaults_to_smolvm_smolvm_smolvm_win_pro() -> None:
    text = render_autounattend()
    assert "<Name>smolvm</Name>" in text
    assert "<Value>smolvm</Value>" in text
    assert "<ComputerName>smolvm-win</ComputerName>" in text
    assert "<Value>Windows 11 Pro</Value>" in text


def test_render_preserves_powershell_braces_literally() -> None:
    """PowerShell `{` and `}` in FirstLogonCommands must survive intact."""
    text = render_autounattend()
    # The virtio-win-guest-tools install loop has literal PowerShell braces
    # — they must not have been mangled by template substitution.
    assert "foreach ($d in 'D','E','F','G','H') {" in text
    assert "if (Test-Path $p) {" in text


# ───────────────────────── build_autounattend_iso ────────────────────────


def test_build_iso_uses_xorrisofs_with_autounattend_label(tmp_path: Path) -> None:
    """xorrisofs is invoked with the canonical AUTOUNATTEND volume label."""
    out = tmp_path / "autounattend.iso"

    def fake_run(cmd: list[str], **_kwargs):
        # Simulate xorrisofs succeeding by touching the output file.
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"ISO9660_PADDING" * 100)
        return MagicMock(returncode=0, stdout="", stderr="")

    with (
        patch("smolvm.windows.build_image.shutil.which", return_value="/usr/bin/xorrisofs"),
        patch("smolvm.windows.build_image.subprocess.run", side_effect=fake_run) as mock_run,
    ):
        result = build_autounattend_iso("<unattend/>", out)

    assert result == out
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "/usr/bin/xorrisofs"
    # Volume label is set to the magic name Windows Setup auto-discovers.
    label_idx = cmd.index("-V")
    assert cmd[label_idx + 1] == _AUTOUNATTEND_VOLUME_LABEL
    # The output path was passed via -o.
    assert cmd[cmd.index("-o") + 1] == str(out)


def test_build_iso_raises_clear_install_hint_when_xorrisofs_missing(
    tmp_path: Path,
) -> None:
    out = tmp_path / "autounattend.iso"
    with (
        patch("smolvm.windows.build_image.shutil.which", return_value=None),
        pytest.raises(Exception, match="xorrisofs is required"),
    ):
        build_autounattend_iso("<unattend/>", out)


# ───────────────────────── WindowsImageBuilder validation ─────────────────


def test_builder_rejects_missing_windows_iso(tmp_path: Path) -> None:
    virtio = tmp_path / "virtio-win.iso"
    virtio.touch()
    out = tmp_path / "win11.qcow2"
    builder = WindowsImageBuilder(
        windows_iso=tmp_path / "does-not-exist.iso",
        virtio_win_iso=virtio,
        output_qcow2=out,
    )
    with pytest.raises(ValueError, match="Windows ISO not found"):
        builder.build()


def test_builder_rejects_missing_virtio_iso(tmp_path: Path) -> None:
    win = tmp_path / "Win11.iso"
    win.touch()
    out = tmp_path / "win11.qcow2"
    builder = WindowsImageBuilder(
        windows_iso=win,
        virtio_win_iso=tmp_path / "does-not-exist.iso",
        output_qcow2=out,
    )
    with pytest.raises(ValueError, match="virtio-win ISO not found"):
        builder.build()


def test_builder_refuses_to_clobber_existing_output(tmp_path: Path) -> None:
    win = tmp_path / "Win11.iso"
    virtio = tmp_path / "virtio-win.iso"
    out = tmp_path / "already-here.qcow2"
    win.touch()
    virtio.touch()
    out.write_bytes(b"existing image bytes")
    builder = WindowsImageBuilder(
        windows_iso=win, virtio_win_iso=virtio, output_qcow2=out
    )
    with pytest.raises(ValueError, match="already exists and is non-empty"):
        builder.build()


def test_builder_passes_through_credentials_and_edition(tmp_path: Path) -> None:
    """Constructor stores values; build() then uses them for rendering."""
    win = tmp_path / "Win11.iso"
    virtio = tmp_path / "virtio-win.iso"
    out = tmp_path / "win11.qcow2"
    win.touch()
    virtio.touch()

    builder = WindowsImageBuilder(
        windows_iso=win,
        virtio_win_iso=virtio,
        output_qcow2=out,
        username="ops",
        password="Hunter2!",
        hostname="ci-win",
        edition="Windows 11 Enterprise",
        disk_size_mib=32 * 1024,
        build_timeout_s=600,
    )
    assert builder.username == "ops"
    assert builder.password == "Hunter2!"
    assert builder.hostname == "ci-win"
    assert builder.edition == "Windows 11 Enterprise"
    assert builder.disk_size_mib == 32 * 1024
    assert builder.build_timeout_s == 600
