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

"""Tests for the ``smolvm setup`` runner."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.host.setup import SetupOptions, build_setup_command, run_setup


def _make_asset_root(tmp_path: Path) -> Path:
    asset_root = tmp_path / "assets"
    (asset_root / "internal").mkdir(parents=True)
    (asset_root / "system-setup.sh").write_text("#!/bin/bash\n")
    (asset_root / "system-setup-macos.sh").write_text("#!/bin/bash\n")
    return asset_root


class TestBuildSetupCommand:
    """Tests for platform-specific setup command construction."""

    def test_linux_default_includes_configure_runtime(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(SetupOptions(), system_name="Linux", asset_root=asset_root)

        assert command == [
            "bash",
            str(asset_root / "system-setup.sh"),
            "--configure-runtime",
        ]

    def test_linux_check_only_keeps_runtime_config_check(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(
            SetupOptions(check_only=True),
            system_name="Linux",
            asset_root=asset_root,
        )

        assert command == [
            "bash",
            str(asset_root / "system-setup.sh"),
            "--check-only",
            "--configure-runtime",
        ]

    def test_linux_no_configure_runtime_removes_flag(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(
            SetupOptions(check_only=True, configure_runtime=False, skip_deps=True),
            system_name="Linux",
            asset_root=asset_root,
        )

        assert command == [
            "bash",
            str(asset_root / "system-setup.sh"),
            "--check-only",
            "--skip-deps",
        ]

    def test_linux_remove_runtime_config_only_forwards_removal_args(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(
            SetupOptions(
                check_only=True,
                with_docker=True,
                configure_runtime=False,
                skip_deps=True,
                runtime_user="aniket",
                remove_runtime_config=True,
            ),
            system_name="Linux",
            asset_root=asset_root,
        )

        assert command == [
            "bash",
            str(asset_root / "system-setup.sh"),
            "--remove-runtime-config",
            "--runtime-user",
            "aniket",
        ]

    def test_macos_uses_macos_script_and_supported_flags_only(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(
            SetupOptions(check_only=True, with_docker=True, configure_runtime=False),
            system_name="Darwin",
            asset_root=asset_root,
        )

        assert command == [
            "bash",
            str(asset_root / "system-setup-macos.sh"),
            "--check-only",
            "--with-docker",
        ]

    def test_macos_skip_deps_forwarded_to_script(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(
            SetupOptions(skip_deps=True),
            system_name="Darwin",
            asset_root=asset_root,
        )

        assert command == [
            "bash",
            str(asset_root / "system-setup-macos.sh"),
            "--skip-deps",
        ]

    def test_linux_for_bake_appends_single_flag(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(
            SetupOptions(for_bake=True, skip_kvm_check=True, skip_runtime_check=True),
            system_name="Linux",
            asset_root=asset_root,
        )

        # --for-bake implies the two skips on the bash side, so we don't
        # double-emit them when for_bake is set.
        assert command == [
            "bash",
            str(asset_root / "system-setup.sh"),
            "--configure-runtime",
            "--for-bake",
        ]

    def test_linux_skip_flags_without_for_bake_pass_through(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(
            SetupOptions(skip_kvm_check=True, skip_runtime_check=True),
            system_name="Linux",
            asset_root=asset_root,
        )

        assert command == [
            "bash",
            str(asset_root / "system-setup.sh"),
            "--configure-runtime",
            "--skip-kvm-check",
            "--skip-runtime-check",
        ]

    def test_linux_firecracker_version_forwarded(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(
            SetupOptions(firecracker_version="v1.15.0"),
            system_name="Linux",
            asset_root=asset_root,
        )

        assert command == [
            "bash",
            str(asset_root / "system-setup.sh"),
            "--configure-runtime",
            "--firecracker-version",
            "v1.15.0",
        ]

    def test_macos_ignores_linux_only_bake_flags(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        command = build_setup_command(
            SetupOptions(
                for_bake=True,
                skip_kvm_check=True,
                skip_runtime_check=True,
                firecracker_version="v1.15.0",
            ),
            system_name="Darwin",
            asset_root=asset_root,
        )

        assert command == [
            "bash",
            str(asset_root / "system-setup-macos.sh"),
        ]

    def test_unsupported_os_fails(self, tmp_path: Path) -> None:
        asset_root = _make_asset_root(tmp_path)

        with pytest.raises(RuntimeError, match="supported only on Linux and macOS"):
            build_setup_command(SetupOptions(), system_name="Windows", asset_root=asset_root)

    def test_missing_asset_fails(self, tmp_path: Path) -> None:
        asset_root = tmp_path / "assets"
        asset_root.mkdir()

        with pytest.raises(FileNotFoundError, match="Missing packaged setup asset"):
            build_setup_command(SetupOptions(), system_name="Linux", asset_root=asset_root)


class TestRunSetup:
    """Tests for subprocess execution behavior."""

    @patch("smolvm.host.setup.subprocess.run")
    def test_child_exit_code_is_propagated(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        asset_root = _make_asset_root(tmp_path)
        mock_run.return_value = subprocess.CompletedProcess(
            args=["bash", "system-setup.sh"],
            returncode=7,
        )

        exit_code = run_setup(SetupOptions(), system_name="Linux", asset_root=asset_root)

        assert exit_code == 7

    @patch("smolvm.host.setup.subprocess.run")
    def test_run_setup_inherits_stdio_and_does_not_capture_output(
        self,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        asset_root = _make_asset_root(tmp_path)
        mock_run.return_value = subprocess.CompletedProcess(
            args=["bash", "system-setup.sh"],
            returncode=0,
        )

        run_setup(SetupOptions(with_docker=True), system_name="Darwin", asset_root=asset_root)

        mock_run.assert_called_once_with(
            [
                "bash",
                str(asset_root / "system-setup-macos.sh"),
                "--with-docker",
            ],
            check=False,
        )
