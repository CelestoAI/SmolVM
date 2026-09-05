# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from smolvm.cli.commands.app import build_cli
from smolvm.cli.image import _build_macos_image_with_progress
from smolvm.cli.main import _run_desktop
from smolvm.macos.models import MacOSInstallProgress
from smolvm.types import DesktopEndpoint, VMState


def test_desktop_command_forwards_start_and_json_options() -> None:
    with patch("smolvm.cli.main._run_desktop", return_value=0) as handler:
        result = CliRunner().invoke(
            build_cli(),
            ["sandbox", "desktop", "mac-test", "--start", "--json"],
        )

    assert result.exit_code == 0
    args = handler.call_args.args[0]
    assert args.vm_id == "mac-test"
    assert args.start is True
    assert args.json is True
    assert args.command_name == "sandbox.desktop"


def test_setup_macos_installs_pinned_runtime(tmp_path) -> None:  # type: ignore[no-untyped-def]
    binary = tmp_path / "lume"
    with patch("smolvm.host.lume.install_pinned_lume", return_value=binary) as install:
        result = CliRunner().invoke(build_cli(), ["setup", "--macos"])

    assert result.exit_code == 0
    assert "Installed macOS sandbox runtime" in result.output
    install.assert_called_once()


def test_image_build_routes_macos_to_local_ipsw_builder() -> None:
    with patch("smolvm.cli.image.run_macos_image_build", return_value=0) as build:
        result = CliRunner().invoke(
            build_cli(),
            ["image", "build", "--os", "macos", "--ipsw", "latest", "-t", "macos-latest"],
        )

    assert result.exit_code == 0
    build.assert_called_once()
    assert build.call_args.kwargs["tag"] == "macos-latest"
    assert build.call_args.kwargs["ipsw"] == "latest"


def test_macos_image_build_rejects_explicit_linux_options() -> None:
    for option in (["--size-mb", "512"], ["--size-mb", "1024"], ["--backend", "auto"]):
        with patch("smolvm.cli.image.run_macos_image_build", return_value=0) as build:
            result = CliRunner().invoke(
                build_cli(),
                [
                    "image",
                    "build",
                    "--os",
                    "macos",
                    "--ipsw",
                    "latest",
                    "-t",
                    "macos-latest",
                    *option,
                ],
            )

        assert result.exit_code == 2
        assert option[0] in result.output
        assert "smolvm image build --os macos" in result.output
        build.assert_not_called()


def test_macos_image_build_retry_preserves_supported_options(tmp_path: Path) -> None:
    ipsw = tmp_path / "Apple Restore.ipsw"
    image_dir = tmp_path / "image cache"
    with patch("smolvm.cli.image.run_macos_image_build", return_value=0) as build:
        result = CliRunner().invoke(
            build_cli(),
            [
                "image",
                "build",
                "--os",
                "macos",
                "--ipsw",
                str(ipsw),
                "-t",
                "macos-latest",
                "--image-dir",
                str(image_dir),
                "--json",
                "--size-mb",
                "512",
                "--backend",
                "auto",
            ],
            terminal_width=240,
        )

    assert result.exit_code == 2
    assert "--size-mb and --backend are not available" in result.output
    assert (
        "smolvm image build --os macos "
        f"--ipsw '{ipsw}' -t macos-latest --image-dir '{image_dir}' --json"
    ) in result.output
    assert "without them" in result.output
    build.assert_not_called()


def test_macos_image_progress_renders_each_phase(capsys) -> None:  # type: ignore[no-untyped-def]
    manager = MagicMock()
    result_marker = object()

    def build(**kwargs):  # type: ignore[no-untyped-def]
        callback = kwargs["on_progress"]
        callback(MacOSInstallProgress("download", 25))
        callback(MacOSInstallProgress("install", 50))
        callback(MacOSInstallProgress("setup"))
        callback(MacOSInstallProgress("complete", 100))
        return result_marker

    manager.build.side_effect = build

    result = _build_macos_image_with_progress(manager, name="macos-latest", ipsw="latest")

    assert result is result_marker
    assert "macOS image ready" in capsys.readouterr().out


def test_desktop_handler_json_returns_sanitized_endpoint(capsys) -> None:  # type: ignore[no-untyped-def]
    vm = MagicMock()
    vm.vm_id = "mac-test"
    vm.status = VMState.RUNNING
    vm.desktop_endpoint = DesktopEndpoint(port=5901)

    with patch("smolvm.facade.SmolVM.from_id", return_value=vm):
        result = _run_desktop(
            SimpleNamespace(
                command_name="sandbox.desktop",
                vm_id="mac-test",
                start=False,
                boot_timeout=30.0,
                json=True,
            )
        )

    assert result == 0
    output = capsys.readouterr().out
    assert '"viewer_url":"vnc://127.0.0.1:5901"' in output.replace(" ", "")
    assert "password" not in output.lower()
    vm.open_desktop.assert_not_called()
