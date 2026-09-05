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

import os
import shlex
import subprocess
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
    OPENCODE_PRESET,
    PI_PRESET,
    HostConfigCopy,
    HostKeychainSecret,
    Preset,
    apply_preset,
    collect_host_env,
    get_preset,
    list_presets,
    preset_names,
    transfer_host_env,
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
        assert preset_names() == ["claude-code", "codex", "hermes", "openclaw", "opencode", "pi"]

    def test_list_presets_sorted_by_name(self) -> None:
        names = [p.name for p in list_presets()]
        assert names == sorted(names)
        assert {p.name for p in list_presets()} == {
            "codex",
            "claude-code",
            "hermes",
            "openclaw",
            "opencode",
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

    def test_get_preset_returns_opencode(self) -> None:
        assert get_preset("opencode") is OPENCODE_PRESET

    def test_unknown_preset_lists_available(self) -> None:
        with pytest.raises(
            KeyError,
            match="Available: claude-code, codex, hermes, openclaw, opencode, pi",
        ):
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

    def test_codex_config_copy_is_filtered(self) -> None:
        from smolvm.presets.codex import (
            CODEX_CONFIG_EXCLUDE_PATTERNS,
            CODEX_CONFIG_INCLUDE_PATTERNS,
        )

        cfg = next(c for c in CODEX_PRESET.host_configs if c.guest_path == "/root/.codex")

        assert cfg.include_patterns == CODEX_CONFIG_INCLUDE_PATTERNS
        assert {
            "auth.json",
            "config.toml",
            "*.config.toml",
            "hooks.json",
            "AGENTS.md",
            "AGENTS.override.md",
            "rules/**",
            "agents/*.toml",
        }.issubset(cfg.include_patterns)
        assert cfg.exclude_patterns == CODEX_CONFIG_EXCLUDE_PATTERNS
        assert {
            "archived_sessions/**",
            "attachments/**",
            "cache/**",
            "history.jsonl",
            "logs*",
            "models_cache.json",
            "session_index.jsonl",
            "shell_snapshots/**",
            "skills/**",
            "*.sqlite",
            "*.sqlite-*",
            "*.db",
            "*.db-*",
        }.issubset(cfg.exclude_patterns)

    def test_codex_install_runs_npm_install_codex(self) -> None:
        assert "@openai/codex" in CODEX_PRESET.install_script
        assert "npm install -g" in CODEX_PRESET.install_script

    def test_codex_apply_copies_only_auth_and_config_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tarfile
        from dataclasses import replace

        monkeypatch.setenv("HOME", str(tmp_path))
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()

        allowed_files = {
            "auth.json": "{}",
            "config.toml": "model = 'gpt-5'\n",
            "daily.config.toml": "approval_policy = 'on-request'\n",
            "requirements.toml": "[policies]\n",
            "hooks.json": "[]\n",
            "AGENTS.md": "Use concise updates.\n",
            "AGENTS.override.md": "Temporary override.\n",
            "rules/default.rules": "prefix_rule(pattern = ['gh'], decision = 'allow')\n",
            "agents/worker.toml": "name = 'worker'\n",
        }
        for rel, content in allowed_files.items():
            path = codex_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (codex_dir / "auth.json").chmod(0o600)

        bulky_files = (
            "history.jsonl",
            "session_index.jsonl",
            "models_cache.json",
            "logs_2.sqlite",
            "logs_2.sqlite-shm",
            "logs_2.sqlite-wal",
            "state_5.sqlite",
            "state_5.sqlite-wal",
            "token.db",
            "token.db-wal",
            "archived_sessions/rollout.jsonl",
            "attachments/clip/image.png",
            "cache/blob.json",
            "log/codex.log",
            "shell_snapshots/shell.sh",
            "tmp/work/file",
            ".tmp/scratch/file",
            "app-server-control/app-server.log",
            "app-server-daemon/daemon.lock",
            "memories/MEMORY.md",
            "packages/standalone/releases/codex",
            "plugins/cache/plugin.json",
            "plugins/.remote-plugin-install-staging/plugin.json",
            "skills/example/SKILL.md",
            "worktrees/repo/file.py",
        )
        for rel in bulky_files:
            path = codex_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("bulk")

        ssh = MagicMock()
        ssh.run.return_value = _ok()
        staged_tars: list[Path] = []

        def capture_put(local: object, remote: str) -> None:
            path = Path(str(local))
            if remote.endswith(".tar"):
                snapshot = tmp_path / f"codex-{len(staged_tars)}.tar"
                snapshot.write_bytes(path.read_bytes())
                staged_tars.append(snapshot)

        ssh.put_file.side_effect = capture_put
        preset = replace(CODEX_PRESET, setup_script="", install_script="")

        summary = apply_preset(ssh, preset)

        assert summary["copied_configs"] == ["/root/.codex"]
        assert len(staged_tars) == 1
        with tarfile.open(staged_tars[0]) as tf:
            members = tf.getmembers()
        names = {
            member.name.removeprefix("./") for member in members if member.name not in {".", "./"}
        }

        assert set(allowed_files).issubset(names)
        assert set(bulky_files).isdisjoint(names)
        for excluded_root in (
            ".tmp",
            "app-server-control",
            "app-server-daemon",
            "archived_sessions",
            "attachments",
            "cache",
            "log",
            "memories",
            "packages",
            "plugins",
            "shell_snapshots",
            "skills",
            "tmp",
            "worktrees",
        ):
            assert excluded_root not in names
            assert not any(name.startswith(f"{excluded_root}/") for name in names)

        auth_member = next(member for member in members if member.name.endswith("auth.json"))
        assert auth_member.mode & 0o777 == 0o600

    def test_missing_codex_config_dir_skips_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dataclasses import replace

        monkeypatch.setenv("HOME", str(tmp_path))
        ssh = MagicMock()
        ssh.run.return_value = _ok()
        preset = replace(CODEX_PRESET, setup_script="", install_script="")

        summary = apply_preset(ssh, preset)

        assert "/root/.codex" not in summary["copied_configs"]
        ssh.put_file.assert_not_called()


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

    def test_claude_code_forwards_minimized_json_and_on_disk_token(self) -> None:
        """The ~/.claude.json copy must run through ``minimize_claude_json``
        and write 0o600 — the file carries OAuth/onboarding state and the
        host copy is mostly per-host project history we don't want in the
        guest. The ~/.claude *directory* is intentionally not copied, but
        the single Linux on-disk credential file still needs to travel."""
        from smolvm.presets.claude_code import minimize_claude_json

        pairs = [(cfg.host_path, cfg.guest_path) for cfg in CLAUDE_CODE_PRESET.host_configs]
        assert pairs == [
            ("~/.claude.json", "/root/.claude.json"),
            ("~/.claude/.credentials.json", "/root/.claude/.credentials.json"),
        ]

        json_cfg, token_cfg = CLAUDE_CODE_PRESET.host_configs
        assert json_cfg.transform is minimize_claude_json
        assert json_cfg.file_mode == 0o600
        assert token_cfg.transform is None
        assert token_cfg.file_mode == 0o600

    def test_minimize_claude_json_keeps_only_auth_keys(self) -> None:
        """The transform projects the host config down to the auth/
        onboarding allowlist, dropping host-specific bulk (project
        history, caches) and ``installMethod`` (which reflects the host's
        install layout, not the guest's)."""
        import json

        from smolvm.presets.claude_code import minimize_claude_json

        raw = json.dumps(
            {
                "oauthAccount": {"emailAddress": "u@example.com"},
                "userID": "uid-123",
                "hasCompletedOnboarding": True,
                "installMethod": "native",
                "projects": {"/home/u/repo": {"allowedTools": ["x"]}},
                "cachedGrowthBookFeatures": {"big": "blob"},
            }
        ).encode()

        result = json.loads(minimize_claude_json(raw))

        assert result["oauthAccount"] == {"emailAddress": "u@example.com"}
        assert result["userID"] == "uid-123"
        assert result["hasCompletedOnboarding"] is True
        # Host-specific bulk and install layout are dropped.
        assert "installMethod" not in result
        assert "projects" not in result
        assert "cachedGrowthBookFeatures" not in result

    def test_minimize_claude_json_tolerates_malformed_input(self) -> None:
        """A corrupt host ~/.claude.json must not abort provisioning — the
        transform falls back to an empty (valid) JSON object. Valid JSON
        that isn't an object (null, list, string) must fall back too, so
        the allowlist projection never raises on a non-dict."""
        import json

        from smolvm.presets.claude_code import minimize_claude_json

        for raw in (b"{not json", b"null", b"[]", b'"a string"', b"42"):
            assert json.loads(minimize_claude_json(raw)) == {}


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
            ("~/.claude/.credentials.json", "/root/.claude/.credentials.json"),
        ]

    def test_pi_reuses_filtered_codex_copy_policy(self) -> None:
        from smolvm.presets.codex import CODEX_HOST_CONFIGS

        pi_codex_cfg = next(
            cfg for cfg in PI_PRESET.host_configs if cfg.guest_path == "/root/.codex"
        )

        assert pi_codex_cfg == CODEX_HOST_CONFIGS[0]

    def test_pi_pulls_oauth_from_macos_keychain(self) -> None:
        """Pi delegates Claude Pro/Max auth through Claude Code's
        ~/.claude/.credentials.json, so it must reuse the same keychain
        extraction."""
        from smolvm.presets.claude_code import CLAUDE_CODE_KEYCHAIN_SECRET

        assert PI_PRESET.host_keychain_secrets == (CLAUDE_CODE_KEYCHAIN_SECRET,)

    def test_pi_install_runs_npm_install(self) -> None:
        assert "@mariozechner/pi-coding-agent" in PI_PRESET.install_script
        assert "npm install -g" in PI_PRESET.install_script

    def test_pi_claude_copies_use_minimized_json_and_on_disk_token(self) -> None:
        """Pi forwards ~/.claude.json for Claude Pro/Max delegation, so it
        must reuse claude-code's minimizing transform — this drops the
        host-specific ``installMethod`` (along with project history and
        caches) that would otherwise break claude's subscription path.
        It must also forward Linux's on-disk Claude token file."""
        from smolvm.presets.claude_code import minimize_claude_json

        claude_cfg = next(
            cfg for cfg in PI_PRESET.host_configs if cfg.guest_path == "/root/.claude.json"
        )
        token_cfg = next(
            cfg
            for cfg in PI_PRESET.host_configs
            if cfg.guest_path == "/root/.claude/.credentials.json"
        )
        assert claude_cfg.transform is minimize_claude_json
        assert claude_cfg.file_mode == 0o600
        assert token_cfg.transform is None
        assert token_cfg.file_mode == 0o600

    def test_pi_setup_uses_node20_bootstrap(self) -> None:
        from smolvm.presets._scripts import NODE20_BOOTSTRAP

        assert PI_PRESET.setup_script == NODE20_BOOTSTRAP


class TestOpenClawPreset:
    """OpenClaw preset wires up the right keys, paths, and install command."""

    def test_openclaw_preset_shape(self) -> None:
        assert OPENCLAW_PRESET.name == "openclaw"
        assert OPENCLAW_PRESET.aliases == ("claw",)
        assert OPENCLAW_PRESET.launch_command == "openclaw"
        assert OPENCLAW_PRESET.host_env_vars == (
            "OPENROUTER_API_KEY",
            "OPENAI_API_KEY",
            "OPENCLAW_GATEWAY_TOKEN",
            "OPENCLAW_GATEWAY_PASSWORD",
        )

    def test_openclaw_copies_only_portable_config_files(self) -> None:
        pairs = [(cfg.host_path, cfg.guest_path) for cfg in OPENCLAW_PRESET.host_configs]
        assert pairs == [
            ("~/.openclaw/openclaw.json", "/root/.openclaw/openclaw.json"),
            ("~/.openclaw/.env", "/root/.openclaw/.env"),
        ]
        assert all(cfg.file_mode == 0o600 for cfg in OPENCLAW_PRESET.host_configs)

    def test_openclaw_install_is_pinned_and_allows_lifecycle_scripts(self) -> None:
        from smolvm.presets.openclaw import OPENCLAW_VERSION

        assert f"openclaw@{OPENCLAW_VERSION}" in OPENCLAW_PRESET.install_script
        assert "npm install -g" in OPENCLAW_PRESET.install_script
        assert "--allow-scripts=openclaw" in OPENCLAW_PRESET.install_script
        assert f"openclaw --version | grep -F {OPENCLAW_VERSION}" in OPENCLAW_PRESET.install_script

    def test_openclaw_install_fails_when_the_cli_reports_another_version(
        self,
        tmp_path: Path,
    ) -> None:
        npm = tmp_path / "npm"
        npm.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then printf "11.16.0\\n"; fi\n')
        npm.chmod(0o755)
        openclaw = tmp_path / "openclaw"
        openclaw.write_text('#!/bin/sh\nprintf "OpenClaw 2026.8.0\\n"\n')
        openclaw.chmod(0o755)

        result = subprocess.run(
            ["bash", "-c", OPENCLAW_PRESET.install_script],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        )

        assert result.returncode != 0

    def test_openclaw_setup_uses_supported_node24_patch(self) -> None:
        assert "setup_24.x" in OPENCLAW_PRESET.setup_script
        assert "const minimum = [24, 15, 0]" in OPENCLAW_PRESET.setup_script

    def test_openclaw_is_ubuntu_only_and_skips_stale_published_image(self) -> None:
        assert OPENCLAW_PRESET.supported_oses == ("ubuntu",)
        assert OPENCLAW_PRESET.prefer_published_image is False

    def test_openclaw_no_keychain_secrets(self) -> None:
        assert OPENCLAW_PRESET.host_keychain_secrets == ()


class TestOpenCodePreset:
    """Stable OpenCode preset wiring."""

    def test_opencode_preset_shape(self) -> None:
        assert OPENCODE_PRESET.name == "opencode"
        assert OPENCODE_PRESET.launch_command == "opencode"
        assert OPENCODE_PRESET.aliases == ()
        assert "ANTHROPIC_API_KEY" in OPENCODE_PRESET.host_env_vars
        assert "OPENAI_API_KEY" in OPENCODE_PRESET.host_env_vars

    def test_opencode_install_runs_stable_npm_package(self) -> None:
        assert "opencode-ai" in OPENCODE_PRESET.install_script
        assert "npm install -g" in OPENCODE_PRESET.install_script
        assert "@beta" not in OPENCODE_PRESET.install_script

    def test_opencode_copies_config_and_auth(self) -> None:
        pairs = [(cfg.host_path, cfg.guest_path) for cfg in OPENCODE_PRESET.host_configs]
        assert pairs == [
            ("~/.config/opencode", "/root/.config/opencode"),
            ("~/.local/share/opencode/auth.json", "/root/.local/share/opencode/auth.json"),
        ]
        auth = OPENCODE_PRESET.host_configs[1]
        assert auth.file_mode == 0o600


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

    def test_default_install_script_runs_without_lifecycle_option_error(
        self, tmp_path: Path
    ) -> None:
        npm = tmp_path / "npm"
        npm.write_text("#!/bin/sh\nexit 0\n")
        npm.chmod(0o755)

        result = subprocess.run(
            ["bash", "-c", npm_install_global("example-package")],
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        )

        assert result.returncode == 0, result.stderr

    @pytest.mark.parametrize("version", ["latest", "1.2", "1.2.3; echo bad"])
    def test_rejects_unpinned_or_unsafe_versions(self, version: str) -> None:
        with pytest.raises(ValueError, match="unsafe npm package version"):
            npm_install_global("openclaw", version=version)

    def test_accepts_exact_version_and_lifecycle_policy(self) -> None:
        script = npm_install_global("openclaw", version="2026.9.1", allow_scripts=True)

        assert "openclaw@2026.9.1" in script
        assert "--allow-scripts=openclaw" in script
        assert "npm_major" in script

    @pytest.mark.parametrize(
        "npm_version,allows_scripts",
        [("11.15.0", False), ("11.16.0", True), ("12.0.0", True)],
    )
    def test_lifecycle_policy_matches_supported_npm_versions(
        self,
        tmp_path: Path,
        npm_version: str,
        allows_scripts: bool,
    ) -> None:
        npm = tmp_path / "npm"
        npm.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then printf "%s\\n" "$NPM_VERSION"; exit 0; fi\n'
            'printf "%s\\n" "$@" >> "$NPM_LOG"\n'
        )
        npm.chmod(0o755)
        log = tmp_path / "npm.log"

        result = subprocess.run(
            [
                "bash",
                "-c",
                npm_install_global("openclaw", version="2026.9.1", allow_scripts=True),
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
                "NPM_LOG": str(log),
                "NPM_VERSION": npm_version,
            },
        )

        assert result.returncode == 0, result.stderr
        install_args = log.read_text().splitlines()
        assert ("--allow-scripts=openclaw" in install_args) is allows_scripts


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
        assert "current[0] > 22" in script

    def test_node_bootstrap_can_pin_a_minimum_patch_within_a_major(self) -> None:
        from smolvm.presets._scripts import node_bootstrap

        script = node_bootstrap(24, minimum_version=(24, 15, 0))

        assert "setup_24.x" in script
        assert "const minimum = [24, 15, 0]" in script
        assert "current[0] === 24" in script

    def test_node_bootstrap_supports_alpine_packages(self) -> None:
        from smolvm.presets._scripts import node_bootstrap

        script = node_bootstrap(20)
        assert "command -v apk" in script
        assert "apk add --no-cache" in script
        assert "nodejs npm" in script
        assert "libgcc" not in script
        assert "libstdc++" not in script
        assert "ripgrep" not in script
        assert "This Alpine image does not provide a compatible Node.js version" in script
        assert "Run '$SMOLVM_NODE_RECOVERY_COMMAND'" in script

    def test_node_bootstrap_rejects_too_low(self) -> None:
        from smolvm.presets._scripts import node_bootstrap

        with pytest.raises(ValueError, match="Unsupported Node major version"):
            node_bootstrap(10)

    def test_node_bootstrap_rejects_mismatched_minimum_major(self) -> None:
        from smolvm.presets._scripts import node_bootstrap

        with pytest.raises(ValueError, match="does not match major"):
            node_bootstrap(24, minimum_version=(22, 12, 0))


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


class TestTransferHostEnv:
    """Forwarding works for both modern and fallback control channels."""

    def test_uses_managed_env_when_supported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        channel = MagicMock()
        channel.supports.return_value = True
        channel.set_managed_env.return_value = {"OPENAI_API_KEY": "sk-test"}

        keys = transfer_host_env(channel, CODEX_PRESET)

        assert keys == ["OPENAI_API_KEY"]
        channel.set_managed_env.assert_called_once_with({"OPENAI_API_KEY": "sk-test"})

    def test_falls_back_to_profile_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        channel = MagicMock()
        channel.supports.return_value = False
        channel.run.return_value = _ok()

        keys = transfer_host_env(channel, CODEX_PRESET)

        assert keys == ["OPENAI_API_KEY"]
        assert any("smolvm_env.sh" in call.args[0] for call in channel.run.call_args_list)


class TestApplyPreset:
    """Integration of file copy + env injection + install over a mocked channel."""

    def _make_preset(
        self,
        *,
        setup: str = "",
        install: str = "true",
        host_env_vars: tuple[str, ...] = (),
        host_configs: tuple[HostConfigCopy, ...] = (),
        host_keychain_secrets: tuple[HostKeychainSecret, ...] = (),
    ) -> Preset:
        return Preset(
            name="test",
            summary="test preset",
            setup_script=setup,
            install_script=install,
            host_env_vars=host_env_vars,
            host_configs=host_configs,
            host_keychain_secrets=host_keychain_secrets,
        )

    def test_install_runs_before_copy_and_credentials(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import smolvm.presets._install as install_mod

        events: list[str] = []
        ssh = MagicMock()
        preset = self._make_preset(
            setup="setup command",
            install="install command",
            host_env_vars=("MY_KEY",),
        )
        monkeypatch.setattr(
            install_mod,
            "_run_install_phase",
            lambda *_args, phase, **_kwargs: events.append(phase),
        )
        monkeypatch.setattr(
            install_mod,
            "transfer_host_configs",
            lambda *_args, **_kwargs: events.append("configs") or ["/root/config"],
        )
        monkeypatch.setattr(
            install_mod,
            "transfer_keychain_secrets",
            lambda *_args, **_kwargs: events.append("keychain") or ["/root/secret"],
        )
        monkeypatch.setattr(
            install_mod,
            "transfer_host_env",
            lambda *_args, **_kwargs: events.append("environment") or ["MY_KEY"],
        )
        monkeypatch.setattr(
            install_mod,
            "register_workspace_safe_directories",
            lambda *_args, **_kwargs: events.append("safe-directories"),
        )

        summary = apply_preset(ssh, preset)

        assert summary["preset"] == "test"
        assert summary["injected_env_keys"] == ["MY_KEY"]
        assert summary["copied_configs"] == ["/root/config"]
        assert events == [
            "setup",
            "install",
            "configs",
            "keychain",
            "environment",
            "safe-directories",
        ]

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

        with pytest.raises(SmolVMError, match="isn't there. Restore it"):
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

        # The directory is staged as a tarball, uploaded under /root, and
        # extracted with failure-safe cleanup.
        ssh.put_file.assert_called_once()
        upload_target = ssh.put_file.call_args.args[1]
        assert upload_target.startswith("/root/.smolvm-preset-")
        assert upload_target.endswith(".tar")

        commands_run = [call.args[0] for call in ssh.run.call_args_list]
        extract_command = next(
            cmd for cmd in commands_run if "tar -xf" in cmd and "/root/.claude" in cmd
        )
        assert "chmod 600" in extract_command
        assert "trap 'rm -f" in extract_command

    def test_install_failure_raises_before_credentials_are_transferred(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ssh = MagicMock()
        ssh.run.return_value = _fail(stderr="line1\nline2\nE: bad apt key\n")
        monkeypatch.setenv("FOO_KEY", "1")
        preset = self._make_preset(
            install="apt-get install -y bogus-pkg",
            host_env_vars=("FOO_KEY",),
        )

        with pytest.raises(SmolVMError, match="install failed"):
            apply_preset(ssh, preset)

        assert len(ssh.run.call_args_list) == 1
        assert "smolvm_env.sh" not in ssh.run.call_args.args[0]

    def test_setup_receives_exact_node_recovery_command(self) -> None:
        from smolvm.presets._scripts import node_bootstrap

        ssh = MagicMock()
        ssh.run.return_value = _ok()
        preset = self._make_preset(setup=node_bootstrap(20), install="")

        apply_preset(
            ssh,
            preset,
            preset_command="claude",
            sandbox_name="sbx-claude",
        )

        command = shlex.split(ssh.run.call_args_list[0].args[0])[2]
        assert (
            "SMOLVM_NODE_RECOVERY_COMMAND='smolvm claude start --name sbx-claude --os ubuntu'"
            in command
        )
        assert "Run '$SMOLVM_NODE_RECOVERY_COMMAND'" in command

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
        ssh_config = next(c for c in GIT_HOST_CONFIGS if c.host_path == "~/.ssh")
        assert ssh_config.exclude_patterns == ("authorized_keys", "authorized_keys2")

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

    def test_git_ssh_copy_preserves_guest_authorized_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Host login policy must not replace SmolVM's guest access key."""
        import tarfile

        monkeypatch.setenv("HOME", str(tmp_path))
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        (ssh_dir / "id_ed25519").write_text("PRIVATE")
        (ssh_dir / "authorized_keys").write_text("HOST-INCOMING-KEY\n")
        (ssh_dir / "authorized_keys2").write_text("HOST-LEGACY-INCOMING-KEY\n")

        ssh = MagicMock()
        ssh.run.return_value = _ok()
        staged_tars: list[Path] = []

        def capture_put(local: object, _remote: str) -> None:
            path = Path(str(local))
            if path.suffix == ".tar":
                snapshot = tmp_path / f"authorized-keys-{len(staged_tars)}.tar"
                snapshot.write_bytes(path.read_bytes())
                staged_tars.append(snapshot)

        ssh.put_file.side_effect = capture_put

        apply_preset(ssh, self._stub_codex_preset())

        assert staged_tars, "no SSH archive was staged"
        with tarfile.open(staged_tars[0]) as tf:
            names = {member.name.removeprefix("./").rstrip("/") for member in tf.getmembers()}
        assert "id_ed25519" in names
        assert "authorized_keys" not in names
        assert "authorized_keys2" not in names

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
