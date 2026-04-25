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

"""Tests for smolvm.presets — agent-harness blueprints and applier."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.presets import (
    CLAUDE_CODE_PRESET,
    CODEX_PRESET,
    HostConfigCopy,
    Preset,
    apply_preset,
    collect_host_env,
    get_preset,
    list_presets,
    preset_names,
)
from smolvm.presets._scripts import npm_install_global
from smolvm.types import CommandResult


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "boom") -> CommandResult:
    return CommandResult(exit_code=1, stdout="", stderr=stderr)


class TestRegistry:
    """Built-in preset registration."""

    def test_codex_and_claude_code_registered(self) -> None:
        assert preset_names() == ["claude-code", "codex"]

    def test_list_presets_sorted_by_name(self) -> None:
        names = [p.name for p in list_presets()]
        assert names == sorted(names)
        assert {p.name for p in list_presets()} == {"codex", "claude-code"}

    def test_get_preset_returns_codex(self) -> None:
        assert get_preset("codex") is CODEX_PRESET

    def test_get_preset_returns_claude_code(self) -> None:
        assert get_preset("claude-code") is CLAUDE_CODE_PRESET

    def test_unknown_preset_lists_available(self) -> None:
        with pytest.raises(KeyError, match="Available: claude-code, codex"):
            get_preset("hermes")


class TestCodexPreset:
    """Codex preset wires up the right keys, paths, and install command."""

    def test_codex_preset_shape(self) -> None:
        assert CODEX_PRESET.name == "codex"
        assert CODEX_PRESET.host_env_vars == ("OPENAI_API_KEY",)
        assert any(
            cfg.host_path == "~/.codex" and cfg.guest_path == "/root/.codex"
            for cfg in CODEX_PRESET.host_configs
        )

    def test_codex_install_runs_npm_install_codex(self) -> None:
        assert "@openai/codex" in CODEX_PRESET.install_script
        assert "npm install -g" in CODEX_PRESET.install_script


class TestClaudeCodePreset:
    """Claude Code preset wires up the right keys, paths, and install command."""

    def test_claude_code_preset_shape(self) -> None:
        assert CLAUDE_CODE_PRESET.name == "claude-code"
        assert CLAUDE_CODE_PRESET.host_env_vars == ("ANTHROPIC_API_KEY",)

    def test_claude_code_install_runs_npm_install(self) -> None:
        assert "@anthropic-ai/claude-code" in CLAUDE_CODE_PRESET.install_script
        assert "npm install -g" in CLAUDE_CODE_PRESET.install_script


class TestNpmInstallGlobalSafety:
    """The npm install helper rejects unsafe package names."""

    @pytest.mark.parametrize(
        "name",
        [
            "evil; rm -rf /",
            "pkg && curl bad.example",
            "name with spaces",
            "@scope/with;injection",
        ],
    )
    def test_rejects_shell_metacharacters(self, name: str) -> None:
        with pytest.raises(ValueError, match="unsafe npm package name"):
            npm_install_global(name)

    @pytest.mark.parametrize(
        "name",
        [
            "lodash",
            "@openai/codex",
            "@anthropic-ai/claude-code",
            "some-pkg.v2",
        ],
    )
    def test_accepts_safe_names(self, name: str) -> None:
        script = npm_install_global(name)
        assert name in script


class TestCollectHostEnv:
    """Forwarding host env vars listed in a preset."""

    def test_collects_only_listed_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("UNRELATED", "ignored")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        env = collect_host_env(CODEX_PRESET)

        assert env == {"OPENAI_API_KEY": "sk-test"}

    def test_skips_empty_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "")

        assert collect_host_env(CODEX_PRESET) == {}

    def test_skips_missing_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        assert collect_host_env(CODEX_PRESET) == {}


class TestApplyPreset:
    """Integration of file copy + env injection + install over a mocked SSH."""

    def _make_preset(
        self,
        *,
        install: str = "true",
        host_env_vars: tuple[str, ...] = (),
        host_configs: tuple[HostConfigCopy, ...] = (),
    ) -> Preset:
        return Preset(
            name="test",
            summary="test preset",
            install_script=install,
            host_env_vars=host_env_vars,
            host_configs=host_configs,
        )

    def test_install_runs_after_copy_and_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("MY_KEY", "secret")
        ssh = MagicMock()
        # read_env_vars (called by inject_env_vars in merge mode) should
        # return an empty dict (no existing env).
        ssh.run.return_value = _ok()

        preset = self._make_preset(host_env_vars=("MY_KEY",))

        summary = apply_preset(ssh, preset)

        assert summary["preset"] == "test"
        assert summary["injected_env_keys"] == ["MY_KEY"]
        assert summary["copied_configs"] == []
        # install command was run
        commands_run = [call.args[0] for call in ssh.run.call_args_list]
        assert any("true" in cmd for cmd in commands_run)

    def test_skips_missing_optional_config(
        self,
        tmp_path: Path,
    ) -> None:
        ssh = MagicMock()
        ssh.run.return_value = _ok()

        preset = self._make_preset(
            host_configs=(
                HostConfigCopy(
                    host_path=str(tmp_path / "does-not-exist"),
                    guest_path="/root/missing",
                    required=False,
                ),
            ),
        )

        summary = apply_preset(ssh, preset)

        assert summary["copied_configs"] == []
        ssh.put_file.assert_not_called()

    def test_required_missing_config_raises(self, tmp_path: Path) -> None:
        ssh = MagicMock()

        preset = self._make_preset(
            install="",
            host_configs=(
                HostConfigCopy(
                    host_path=str(tmp_path / "missing"),
                    guest_path="/root/needed",
                    required=True,
                ),
            ),
        )

        with pytest.raises(SmolVMError, match="Required host config not found"):
            apply_preset(ssh, preset)

    def test_copies_file_via_put_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text("k = 1\n")

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        preset = self._make_preset(
            install="",
            host_configs=(
                HostConfigCopy(host_path=str(cfg), guest_path="/root/.codex/config.toml"),
            ),
        )

        summary = apply_preset(ssh, preset)

        assert summary["copied_configs"] == ["/root/.codex/config.toml"]
        # mkdir parent + put_file the file itself
        mkdir_calls = [call.args[0] for call in ssh.run.call_args_list if "mkdir" in call.args[0]]
        assert any("/root/.codex" in cmd for cmd in mkdir_calls)
        ssh.put_file.assert_called_once()
        put_args = ssh.put_file.call_args
        assert str(put_args.args[0]) == str(cfg)
        assert put_args.args[1] == "/root/.codex/config.toml"

    def test_copies_directory_as_tar(self, tmp_path: Path) -> None:
        src = tmp_path / "claude"
        src.mkdir()
        (src / "settings.json").write_text("{}")
        (src / "subdir").mkdir()
        (src / "subdir" / "log.txt").write_text("ok")

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        preset = self._make_preset(
            install="",
            host_configs=(HostConfigCopy(host_path=str(src), guest_path="/root/.claude"),),
        )

        apply_preset(ssh, preset)

        # The directory is staged as a tarball, scp'd, and extracted on the guest.
        ssh.put_file.assert_called_once()
        upload_target = ssh.put_file.call_args.args[1]
        assert upload_target.startswith("/tmp/.smolvm-preset-")
        assert upload_target.endswith(".tar")

        commands_run = [call.args[0] for call in ssh.run.call_args_list]
        assert any("tar -xf" in cmd and "/root/.claude" in cmd for cmd in commands_run)

    def test_install_failure_raises_with_stderr_tail(self) -> None:
        ssh = MagicMock()
        # First .run for inject_env (read_env_vars) succeeds, then install fails.
        ssh.run.side_effect = [
            _ok(),  # read_env_vars in inject_env_vars
            _ok(),  # _atomic_write inside inject_env_vars
            _fail(stderr="line1\nline2\nE: bad apt key\n"),  # install script
        ]

        import os

        os.environ["FOO_KEY"] = "1"
        try:
            preset = self._make_preset(
                install="apt-get install -y bogus-pkg",
                host_env_vars=("FOO_KEY",),
            )
            with pytest.raises(SmolVMError, match="install failed"):
                apply_preset(ssh, preset)
        finally:
            os.environ.pop("FOO_KEY", None)

    def test_progress_callback_receives_steps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        cfg = tmp_path / "config"
        cfg.write_text("x")
        monkeypatch.setenv("MY_KEY", "v")

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        messages: list[str] = []

        preset = self._make_preset(
            install="echo done",
            host_env_vars=("MY_KEY",),
            host_configs=(HostConfigCopy(host_path=str(cfg), guest_path="/root/c"),),
        )

        apply_preset(ssh, preset, on_progress=messages.append)

        # We expect at least: copy, inject env, install
        assert any("Copying" in m for m in messages)
        assert any("env var" in m for m in messages)
        assert any("Installing test" in m for m in messages)
