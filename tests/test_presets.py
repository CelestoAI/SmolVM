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
    GIT_HOST_CONFIGS,
    HERMES_PRESET,
    OPENCLAW_PRESET,
    PI_PRESET,
    HostConfigCopy,
    HostKeychainSecret,
    Preset,
    apply_preset,
    collect_host_env,
    get_preset,
    list_presets,
    preset_names,
)
from smolvm.presets._scripts import npm_install_global, uv_install_global
from smolvm.types import CommandResult


def _ok(stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(exit_code=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "boom") -> CommandResult:
    return CommandResult(exit_code=1, stdout="", stderr=stderr)


@pytest.fixture(autouse=True)
def _isolate_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin ``$HOME`` to a clean tmp dir for every test in this module.

    ``apply_preset`` always layers ``GIT_HOST_CONFIGS`` (``~/.gitconfig``,
    ``~/.ssh``, ``~/.config/gh``, …) onto each preset's own copies. If
    the test runner's real home has any of those files, the unrelated
    ``copied_configs`` / ``ssh.put_file`` assertions in this file pick
    them up as extra entries and flake. Tests that need a populated
    home call ``monkeypatch.setenv('HOME', ...)`` themselves; the later
    setenv on the same monkeypatch instance overrides this default."""
    home = tmp_path_factory.mktemp("isolated_home")
    monkeypatch.setenv("HOME", str(home))


class TestRegistry:
    """Built-in preset registration."""

    def test_builtin_presets_registered(self) -> None:
        assert preset_names() == ["claude-code", "codex", "hermes", "openclaw", "pi"]

    def test_list_presets_sorted_by_name(self) -> None:
        names = [p.name for p in list_presets()]
        assert names == sorted(names)
        assert {p.name for p in list_presets()} == {
            "codex",
            "claude-code",
            "hermes",
            "openclaw",
            "pi",
        }

    def test_get_preset_returns_codex(self) -> None:
        assert get_preset("codex") is CODEX_PRESET

    def test_get_preset_returns_claude_code(self) -> None:
        assert get_preset("claude-code") is CLAUDE_CODE_PRESET

    def test_get_preset_returns_pi(self) -> None:
        assert get_preset("pi") is PI_PRESET

    def test_get_preset_returns_openclaw(self) -> None:
        assert get_preset("openclaw") is OPENCLAW_PRESET

    def test_get_preset_returns_hermes(self) -> None:
        assert get_preset("hermes") is HERMES_PRESET

    def test_unknown_preset_lists_available(self) -> None:
        with pytest.raises(KeyError, match="Available: claude-code, codex, hermes, openclaw, pi"):
            get_preset("nonexistent")


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

    def test_claude_code_pulls_oauth_from_macos_keychain(self) -> None:
        """The preset must declare a keychain secret for the OAuth tokens.

        Claude Code on macOS keeps tokens in the keychain (not in
        ``~/.claude/.credentials.json``); without this entry the guest
        sees the user's profile but says "Not logged in"."""
        from smolvm.presets.claude_code import CLAUDE_CODE_KEYCHAIN_SECRET

        assert CLAUDE_CODE_PRESET.host_keychain_secrets == (CLAUDE_CODE_KEYCHAIN_SECRET,)
        assert CLAUDE_CODE_KEYCHAIN_SECRET.service == "Claude Code-credentials"
        assert CLAUDE_CODE_KEYCHAIN_SECRET.guest_path == "/root/.claude/.credentials.json"

    def test_claude_code_install_runs_npm_install(self) -> None:
        assert "@anthropic-ai/claude-code" in CLAUDE_CODE_PRESET.install_script
        assert "npm install -g" in CLAUDE_CODE_PRESET.install_script

    def test_claude_code_install_strips_host_install_method(self, tmp_path: Path) -> None:
        """The install script must drop ``installMethod`` from the copied
        ``~/.claude.json``. The host's value (e.g. ``"native"`` after the
        user ran ``claude migrate-installer`` on their machine) does not
        match the guest, where claude is npm-installed under
        ``/usr/lib/node_modules`` — leaving it makes claude error with
        ``claude command not found at /root/.local/bin/claude`` on the
        first launch."""
        import json
        import subprocess

        root = tmp_path / "root"
        root.mkdir()
        config = root / ".claude.json"
        config.write_text(
            json.dumps(
                {
                    "installMethod": "native",
                    "theme": "dark",
                    "recentProjects": ["a", "b"],
                }
            )
        )

        # Extract just the cleanup snippet (after the npm install line)
        # and rewrite the hard-coded /root/ path to our temp dir.
        from smolvm.presets.claude_code import CLAUDE_RESET_INSTALL_METHOD

        snippet = CLAUDE_RESET_INSTALL_METHOD.replace("/root/", f"{root}/")
        completed = subprocess.run(
            ["bash", "-c", f"set -euo pipefail; {snippet}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr

        data = json.loads(config.read_text())
        assert "installMethod" not in data
        # Other fields are untouched.
        assert data["theme"] == "dark"
        assert data["recentProjects"] == ["a", "b"]

    def test_claude_code_install_skips_when_config_absent(self, tmp_path: Path) -> None:
        """The cleanup must be a no-op when ~/.claude.json was never
        copied (e.g. a fresh user with no host config)."""
        import subprocess

        from smolvm.presets.claude_code import CLAUDE_RESET_INSTALL_METHOD

        # Point at an empty dir — the file does not exist.
        snippet = CLAUDE_RESET_INSTALL_METHOD.replace("/root/", f"{tmp_path}/")
        completed = subprocess.run(
            ["bash", "-c", f"set -euo pipefail; {snippet}"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


class TestPiPreset:
    """Pi preset wires up the right keys, paths, and install command."""

    def test_pi_preset_shape(self) -> None:
        assert PI_PRESET.name == "pi"
        assert PI_PRESET.aliases == ()
        assert PI_PRESET.launch_command == "pi"
        assert PI_PRESET.host_env_vars == ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")

    def test_pi_forwards_union_of_provider_credentials(self) -> None:
        """Pi reuses on-disk credentials from codex and claude-code in
        addition to its own ~/.pi config, so a prior `codex login` or
        `claude login` on the host carries through into the guest.

        Compared as an ordered list of (host, guest) pairs so a
        duplicate ``HostConfigCopy`` cannot silently dedupe through a
        dict-based assertion."""
        pairs = [(cfg.host_path, cfg.guest_path) for cfg in PI_PRESET.host_configs]
        assert pairs == [
            ("~/.pi", "/root/.pi"),
            ("~/.codex", "/root/.codex"),
            ("~/.claude.json", "/root/.claude.json"),
            ("~/.claude", "/root/.claude"),
        ]

    def test_pi_pulls_oauth_from_macos_keychain(self) -> None:
        """Pi delegates Claude Pro/Max auth through Claude Code's
        ~/.claude/.credentials.json, so it must reuse the same keychain
        extraction."""
        from smolvm.presets.claude_code import CLAUDE_CODE_KEYCHAIN_SECRET

        assert PI_PRESET.host_keychain_secrets == (CLAUDE_CODE_KEYCHAIN_SECRET,)

    def test_pi_install_runs_npm_install(self) -> None:
        assert "@mariozechner/pi-coding-agent" in PI_PRESET.install_script
        assert "npm install -g" in PI_PRESET.install_script

    def test_pi_install_strips_claude_install_method(self) -> None:
        """Pi forwards ~/.claude.json, so its install must append the
        same installMethod cleanup that claude-code uses — otherwise
        the host's ``installMethod`` value carries into the guest and
        breaks claude/Pi's claude-subscription path on first launch."""
        from smolvm.presets.claude_code import CLAUDE_RESET_INSTALL_METHOD

        assert CLAUDE_RESET_INSTALL_METHOD in PI_PRESET.install_script

    def test_pi_setup_uses_node20_bootstrap(self) -> None:
        from smolvm.presets._scripts import NODE20_BOOTSTRAP

        assert PI_PRESET.setup_script == NODE20_BOOTSTRAP


class TestOpenClawPreset:
    """OpenClaw preset wires up the right keys, paths, and install command."""

    def test_openclaw_preset_shape(self) -> None:
        assert OPENCLAW_PRESET.name == "openclaw"
        assert OPENCLAW_PRESET.aliases == ("claw",)
        assert OPENCLAW_PRESET.launch_command == "openclaw"
        assert OPENCLAW_PRESET.host_env_vars == ("OPENROUTER_API_KEY", "OPENAI_API_KEY")

    def test_openclaw_copies_config_dir(self) -> None:
        pairs = [(cfg.host_path, cfg.guest_path) for cfg in OPENCLAW_PRESET.host_configs]
        assert pairs == [("~/.openclaw", "/root/.openclaw")]

    def test_openclaw_install_runs_npm_install(self) -> None:
        assert "openclaw" in OPENCLAW_PRESET.install_script
        assert "npm install -g" in OPENCLAW_PRESET.install_script

    def test_openclaw_setup_uses_node22(self) -> None:
        assert "setup_22.x" in OPENCLAW_PRESET.setup_script
        assert "-ge 22" in OPENCLAW_PRESET.setup_script

    def test_openclaw_no_keychain_secrets(self) -> None:
        assert OPENCLAW_PRESET.host_keychain_secrets == ()


class TestHermesPreset:
    """Hermes preset wires up the right keys, paths, and install command."""

    def test_hermes_preset_shape(self) -> None:
        assert HERMES_PRESET.name == "hermes"
        assert HERMES_PRESET.aliases == ()
        assert HERMES_PRESET.launch_command == "hermes"
        assert HERMES_PRESET.host_env_vars == (
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "HF_TOKEN",
        )

    def test_hermes_copies_config_dir(self) -> None:
        pairs = [(cfg.host_path, cfg.guest_path) for cfg in HERMES_PRESET.host_configs]
        assert pairs == [("~/.hermes", "/root/.hermes")]

    def test_hermes_install_clones_and_pip_installs(self) -> None:
        assert "git clone" in HERMES_PRESET.install_script
        assert "NousResearch/hermes-agent" in HERMES_PRESET.install_script
        assert "uv venv" in HERMES_PRESET.install_script
        assert "uv pip install" in HERMES_PRESET.install_script

    def test_hermes_setup_installs_python(self) -> None:
        assert "python3" in HERMES_PRESET.setup_script
        assert "uv" in HERMES_PRESET.setup_script

    def test_hermes_disk_bumped(self) -> None:
        assert HERMES_PRESET.default_disk_mib == 10240

    def test_hermes_no_keychain_secrets(self) -> None:
        assert HERMES_PRESET.host_keychain_secrets == ()


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


class TestUvInstallGlobalSafety:
    """The uv install helper rejects unsafe package names."""

    @pytest.mark.parametrize(
        "name",
        [
            "evil; rm -rf /",
            "pkg && curl bad.example",
            "name with spaces",
        ],
    )
    def test_rejects_shell_metacharacters(self, name: str) -> None:
        with pytest.raises(ValueError, match="unsafe PyPI package name"):
            uv_install_global(name)

    @pytest.mark.parametrize(
        "name",
        [
            "hermes-agent",
            "some-package.v2",
            "package_name",
        ],
    )
    def test_accepts_safe_names(self, name: str) -> None:
        script = uv_install_global(name)
        assert name in script


class TestNodeBootstrapFunction:
    """The parameterized node_bootstrap() helper."""

    def test_node_bootstrap_20_matches_legacy_constant(self) -> None:
        from smolvm.presets._scripts import NODE20_BOOTSTRAP, node_bootstrap

        assert node_bootstrap(20) == NODE20_BOOTSTRAP

    def test_node_bootstrap_22_uses_correct_version(self) -> None:
        from smolvm.presets._scripts import node_bootstrap

        script = node_bootstrap(22)
        assert "setup_22.x" in script
        assert "-ge 22" in script

    def test_node_bootstrap_rejects_too_low(self) -> None:
        from smolvm.presets._scripts import node_bootstrap

        with pytest.raises(ValueError, match="Unsupported Node major version"):
            node_bootstrap(10)


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
        host_keychain_secrets: tuple[HostKeychainSecret, ...] = (),
    ) -> Preset:
        return Preset(
            name="test",
            summary="test preset",
            install_script=install,
            host_env_vars=host_env_vars,
            host_configs=host_configs,
            host_keychain_secrets=host_keychain_secrets,
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

        # We expect at least: copy, forward env, install
        assert any("Copying" in m for m in messages)
        assert any("environment variable" in m for m in messages)
        assert any("Installing test" in m for m in messages)


class TestExtractKeychainSecret:
    """``security find-generic-password`` wrapper used on macOS hosts."""

    def test_returns_none_on_non_darwin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import smolvm.presets._install as install_mod

        monkeypatch.setattr(install_mod.sys, "platform", "linux")

        assert install_mod._extract_keychain_secret("anything") is None

    def test_returns_none_when_security_binary_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import smolvm.presets._install as install_mod

        monkeypatch.setattr(install_mod.sys, "platform", "darwin")

        def fake_run(*_args: object, **_kwargs: object) -> object:
            raise FileNotFoundError("security")

        monkeypatch.setattr(install_mod.subprocess, "run", fake_run)

        assert install_mod._extract_keychain_secret("svc") is None

    def test_returns_none_when_lookup_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-zero exit (entry not found, user cancelled the prompt,
        permission denied) must not raise — the caller falls through so
        the user can still authenticate inside the guest."""
        import subprocess as _subprocess

        import smolvm.presets._install as install_mod

        monkeypatch.setattr(install_mod.sys, "platform", "darwin")

        def fake_run(args: list[str], **_kwargs: object) -> _subprocess.CompletedProcess[str]:
            return _subprocess.CompletedProcess(
                args=args, returncode=44, stdout="", stderr="not found"
            )

        monkeypatch.setattr(install_mod.subprocess, "run", fake_run)

        assert install_mod._extract_keychain_secret("missing") is None

    def test_returns_none_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the user walks away from the system prompt we time out and
        skip rather than hanging sandbox provisioning forever."""
        import subprocess as _subprocess

        import smolvm.presets._install as install_mod

        monkeypatch.setattr(install_mod.sys, "platform", "darwin")

        def fake_run(*_args: object, **_kwargs: object) -> object:
            raise _subprocess.TimeoutExpired(cmd="security", timeout=60)

        monkeypatch.setattr(install_mod.subprocess, "run", fake_run)

        assert install_mod._extract_keychain_secret("svc") is None

    def test_returns_password_and_strips_one_trailing_newline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``security -w`` always appends one newline; the value itself
        (a JSON blob for Claude Code) must be returned unchanged."""
        import subprocess as _subprocess

        import smolvm.presets._install as install_mod

        monkeypatch.setattr(install_mod.sys, "platform", "darwin")

        captured: dict[str, list[str]] = {}

        def fake_run(args: list[str], **_kwargs: object) -> _subprocess.CompletedProcess[str]:
            captured["args"] = args
            return _subprocess.CompletedProcess(
                args=args, returncode=0, stdout='{"k":"v"}\n', stderr=""
            )

        monkeypatch.setattr(install_mod.subprocess, "run", fake_run)

        assert install_mod._extract_keychain_secret("My Service") == '{"k":"v"}'
        # Exact CLI shape — service passed via -s, password requested via -w.
        assert captured["args"] == [
            "security",
            "find-generic-password",
            "-s",
            "My Service",
            "-w",
        ]

    def test_account_argument_scopes_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Multiple keychain items may share a service name (claude-code
        files one under ``acct=root`` for MCP tokens and another under
        the macOS user for the main login OAuth). The applier must
        reach the right one by passing ``-a``."""
        import subprocess as _subprocess

        import smolvm.presets._install as install_mod

        monkeypatch.setattr(install_mod.sys, "platform", "darwin")

        captured: dict[str, list[str]] = {}

        def fake_run(args: list[str], **_kwargs: object) -> _subprocess.CompletedProcess[str]:
            captured["args"] = args
            return _subprocess.CompletedProcess(
                args=args, returncode=0, stdout="payload\n", stderr=""
            )

        monkeypatch.setattr(install_mod.subprocess, "run", fake_run)

        install_mod._extract_keychain_secret("My Service", account="alice")

        assert captured["args"] == [
            "security",
            "find-generic-password",
            "-s",
            "My Service",
            "-a",
            "alice",
            "-w",
        ]


class TestApplyPresetKeychain:
    """Keychain step within ``apply_preset``."""

    def _make_preset(self, secrets: tuple[HostKeychainSecret, ...]) -> Preset:
        return Preset(
            name="test",
            summary="test preset",
            install_script="",
            host_keychain_secrets=secrets,
        )

    def test_writes_extracted_secret_to_guest_with_chmod(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ssh = MagicMock()
        ssh.run.return_value = _ok()

        monkeypatch.setattr(
            "smolvm.presets._install._extract_keychain_secret",
            lambda service, *, account=None: '{"oauth":"x"}' if service == "Test Service" else None,
        )

        uploaded: list[tuple[str, str]] = []

        def capture_put(local: object, remote: str) -> None:
            uploaded.append((Path(str(local)).read_text(), remote))

        ssh.put_file.side_effect = capture_put

        preset = self._make_preset(
            (
                HostKeychainSecret(
                    service="Test Service",
                    guest_path="/root/.claude/.credentials.json",
                ),
            )
        )

        summary = apply_preset(ssh, preset)

        assert summary["extracted_keychain_secrets"] == ["/root/.claude/.credentials.json"]
        # Plaintext is uploaded, not echoed via shell.
        assert uploaded == [('{"oauth":"x"}', "/root/.claude/.credentials.json")]
        # Parent dir is created before SFTP, and chmod 600 follows the upload.
        commands_run = [call.args[0] for call in ssh.run.call_args_list]
        assert any("mkdir -p" in cmd and "/root/.claude" in cmd for cmd in commands_run)
        assert any(
            "chmod 600" in cmd and "/root/.claude/.credentials.json" in cmd for cmd in commands_run
        )

    def test_skips_when_extraction_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No keychain entry → no SSH side effects, summary lists nothing."""
        ssh = MagicMock()
        ssh.run.return_value = _ok()

        monkeypatch.setattr(
            "smolvm.presets._install._extract_keychain_secret",
            lambda _service, *, account=None: None,
        )

        preset = self._make_preset((HostKeychainSecret(service="Missing", guest_path="/root/x"),))

        summary = apply_preset(ssh, preset)

        assert summary["extracted_keychain_secrets"] == []
        ssh.put_file.assert_not_called()
        # No chmod for a file we never wrote.
        chmod_calls = [call.args[0] for call in ssh.run.call_args_list if "chmod" in call.args[0]]
        assert chmod_calls == []

    def test_default_account_is_macos_login_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When ``HostKeychainSecret.account`` is None, the applier
        must look up the keychain entry under the current user's login
        — that's the account claude-code uses for the main OAuth."""
        import smolvm.presets._install as install_mod

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        monkeypatch.setattr(install_mod.getpass, "getuser", lambda: "alice")

        seen: dict[str, str | None] = {}

        def fake_extract(service: str, *, account: str | None = None) -> str | None:
            seen["service"] = service
            seen["account"] = account
            return None

        monkeypatch.setattr(install_mod, "_extract_keychain_secret", fake_extract)

        preset = self._make_preset((HostKeychainSecret(service="svc", guest_path="/root/x"),))

        apply_preset(ssh, preset)

        assert seen == {"service": "svc", "account": "alice"}

    def test_explicit_account_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import smolvm.presets._install as install_mod

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        monkeypatch.setattr(install_mod.getpass, "getuser", lambda: "alice")

        seen: dict[str, str | None] = {}

        def fake_extract(service: str, *, account: str | None = None) -> str | None:
            seen["account"] = account
            return None

        monkeypatch.setattr(install_mod, "_extract_keychain_secret", fake_extract)

        preset = self._make_preset(
            (HostKeychainSecret(service="svc", guest_path="/root/x", account="bob"),)
        )

        apply_preset(ssh, preset)

        assert seen["account"] == "bob"

    def test_progress_message_emitted_only_when_secret_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The user shouldn't see a "Copying from keychain" message for
        a secret that wasn't actually found."""
        ssh = MagicMock()
        ssh.run.return_value = _ok()

        # Two secrets: one found, one missing.
        monkeypatch.setattr(
            "smolvm.presets._install._extract_keychain_secret",
            lambda service, *, account=None: "blob" if service == "found" else None,
        )

        preset = self._make_preset(
            (
                HostKeychainSecret(service="found", guest_path="/root/a"),
                HostKeychainSecret(service="missing", guest_path="/root/b"),
            )
        )

        messages: list[str] = []
        apply_preset(ssh, preset, on_progress=messages.append)

        keychain_msgs = [m for m in messages if "keychain" in m]
        assert len(keychain_msgs) == 1
        assert "found" in keychain_msgs[0]
        assert "/root/a" in keychain_msgs[0]


class TestGitCredentialInjection:
    """Every preset start auto-copies the host's git/SSH/gh auth files.

    The applier must layer ``GIT_HOST_CONFIGS`` onto whatever the preset
    declares so a fresh sandbox has working ``git``, ``gh``, and
    ``ssh git@github.com`` without the agent re-authenticating. Missing
    files are skipped silently — a host with no ``~/.gitconfig`` should
    not break ``smolvm codex start``.
    """

    def test_git_host_configs_constant_shape(self) -> None:
        """Pin the contract: which host paths land where, and that all
        entries are optional. Adding/removing a path here is a behavior
        change that should be intentional."""
        pairs = {(c.host_path, c.guest_path) for c in GIT_HOST_CONFIGS}
        assert pairs == {
            ("~/.gitconfig", "/root/.gitconfig"),
            ("~/.config/git/config", "/root/.config/git/config"),
            ("~/.git-credentials", "/root/.git-credentials"),
            ("~/.ssh", "/root/.ssh"),
            ("~/.config/gh", "/root/.config/gh"),
        }
        assert all(c.required is False for c in GIT_HOST_CONFIGS)

    def _seed_git_home(self, home: Path) -> None:
        """Create a host home dir with a representative git auth surface."""
        (home / ".gitconfig").write_text("[user]\n\temail = u@example.com\n")
        (home / ".git-credentials").write_text("https://x:y@github.com\n")
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_ed25519"
        key.write_text("PRIVATE")
        key.chmod(0o600)

    def _stub_codex_preset(self) -> Preset:
        """Codex preset with the install step neutered.

        The preset's real setup/install scripts run apt-via-NodeSource
        and npm, which don't make sense against a MagicMock; replace
        with no-ops so the test focuses on the copy stage.
        """
        from dataclasses import replace

        return replace(CODEX_PRESET, setup_script="", install_script="")

    def _stub_claude_code_preset(self) -> Preset:
        from dataclasses import replace

        return replace(CLAUDE_CODE_PRESET, setup_script="", install_script="")

    def test_codex_apply_copies_git_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        self._seed_git_home(tmp_path)

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        summary = apply_preset(ssh, self._stub_codex_preset())

        copied = set(summary["copied_configs"])  # type: ignore[arg-type]
        assert {
            "/root/.gitconfig",
            "/root/.git-credentials",
            "/root/.ssh",
        }.issubset(copied)

    def test_claude_code_apply_copies_git_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        self._seed_git_home(tmp_path)

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        summary = apply_preset(ssh, self._stub_claude_code_preset())

        copied = set(summary["copied_configs"])  # type: ignore[arg-type]
        assert {
            "/root/.gitconfig",
            "/root/.git-credentials",
            "/root/.ssh",
        }.issubset(copied)

    def test_git_injection_silent_when_files_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host with no git config and no SSH dir must still
        provision cleanly — copied_configs simply omits the missing
        guest paths."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # No files seeded.

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        summary = apply_preset(ssh, self._stub_codex_preset())

        copied = set(summary["copied_configs"])  # type: ignore[arg-type]
        git_guest_paths = {c.guest_path for c in GIT_HOST_CONFIGS}
        assert copied.isdisjoint(git_guest_paths)

    def test_git_ssh_uploaded_via_tar_dir_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``~/.ssh`` must travel through the tar-based dir-copy path so
        the guest's keys end up at 0o600 and sshd accepts them. Verified
        indirectly: the recorded ssh.run includes the ``tar -xf ... -C
        /root/.ssh`` template from ``_copy_dir``."""
        monkeypatch.setenv("HOME", str(tmp_path))
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_ed25519"
        key.write_text("PRIVATE")
        key.chmod(0o600)

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        apply_preset(ssh, self._stub_codex_preset())

        commands_run = [call.args[0] for call in ssh.run.call_args_list]
        assert any("tar -xf" in cmd and "/root/.ssh" in cmd for cmd in commands_run), commands_run

    def test_git_ssh_tar_owner_stripped_to_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tar staged for the guest must zero uid/gid on every entry.

        Guests extract as root with default ``--same-owner``; if host
        uids (e.g. macOS 501:20) survive, ``/root/.ssh/id_ed25519``
        ends up owned by uid 501 and sshd refuses the key with "Bad
        owner or permissions". File modes (the 0o600 we care about)
        must remain intact.
        """
        import tarfile

        monkeypatch.setenv("HOME", str(tmp_path))
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        key = ssh_dir / "id_ed25519"
        key.write_text("PRIVATE")
        key.chmod(0o600)

        ssh = MagicMock()
        ssh.run.return_value = _ok()
        staged_tars: list[Path] = []

        def capture_put(local: object, _remote: str) -> None:
            path = Path(str(local))
            if path.suffix == ".tar":
                # Copy aside before _copy_dir's finally clause unlinks it.
                snapshot = tmp_path / f"snapshot-{len(staged_tars)}.tar"
                snapshot.write_bytes(path.read_bytes())
                staged_tars.append(snapshot)

        ssh.put_file.side_effect = capture_put

        apply_preset(ssh, self._stub_codex_preset())

        assert staged_tars, "no tar archive was staged"
        with tarfile.open(staged_tars[0]) as tf:
            members = tf.getmembers()
        assert members, "tar archive is empty"
        assert all(m.uid == 0 and m.gid == 0 for m in members), [
            (m.name, m.uid, m.gid) for m in members
        ]
        assert all(m.uname == "" and m.gname == "" for m in members), [
            (m.name, m.uname, m.gname) for m in members
        ]
        # Mode bits survive — the SSH private key stays at 0o600.
        key_member = next(m for m in members if m.name.endswith("id_ed25519"))
        assert key_member.mode & 0o777 == 0o600

    def test_git_credentials_chmodded_to_0600_after_upload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``~/.git-credentials`` is plaintext OAuth tokens. SFTP drops
        the file at the server's umask (typically 0644). The applier
        must chmod 0600 after upload so the file does not land
        world-readable inside the guest."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".git-credentials").write_text("https://user:token@github.com\n")

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        apply_preset(ssh, self._stub_codex_preset())

        commands_run = [call.args[0] for call in ssh.run.call_args_list]
        assert any(
            "chmod 600" in cmd and "/root/.git-credentials" in cmd for cmd in commands_run
        ), commands_run

    def test_gitconfig_not_chmodded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``~/.gitconfig`` is conventionally world-readable; only
        credential files get the 0600 treatment. Guards against
        accidentally tightening every file_mode."""
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".gitconfig").write_text("[user]\n\temail = u@example.com\n")

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        apply_preset(ssh, self._stub_codex_preset())

        chmod_targets = [
            cmd
            for cmd in (call.args[0] for call in ssh.run.call_args_list)
            if "chmod" in cmd and "/root/.gitconfig" in cmd
        ]
        assert chmod_targets == [], chmod_targets

    def test_workspace_safe_directory_registered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Workspace mounts are 9p shares preserving host uid/gid, so
        git refuses to operate on them with ``fatal: detected dubious
        ownership`` (CVE-2022-24765). The applier must register the
        mount paths in the guest's global git config so users do not
        hit that error on first ``git status``.

        Verifies three properties at once:
          * Both wildcard patterns are registered (mount point itself
            and any repo nested below it).
          * ``--replace-all`` is used so re-runs collapse to one entry
            per path instead of appending duplicates.
          * The value-pattern is anchored to the exact path so we do
            not clobber unrelated ``safe.directory`` entries the
            user's host gitconfig brought into the guest.
        """
        monkeypatch.setenv("HOME", str(tmp_path))

        ssh = MagicMock()
        ssh.run.return_value = _ok()

        apply_preset(ssh, self._stub_codex_preset())

        commands_run = [call.args[0] for call in ssh.run.call_args_list]
        config_cmds = [cmd for cmd in commands_run if "safe.directory" in cmd]
        assert config_cmds, commands_run
        joined = " ".join(config_cmds)
        assert "--replace-all safe.directory" in joined
        assert "'/workspace*'" in joined
        assert "'/workspace*/**'" in joined
        assert r"'^/workspace\*$'" in joined
        assert r"'^/workspace\*/\*\*$'" in joined
