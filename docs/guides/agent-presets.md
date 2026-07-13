# Start an AI coding agent

A preset starts a fresh sandbox, installs one coding agent, and can carry over the credentials and small configuration files that agent needs. It is the quickest way to give an agent a disposable place to work.

## Start an agent

For example, start Codex:

```bash
smolvm codex start --name codex-work
```

Other available presets use the same pattern:

```bash
smolvm claude start --name claude-work
smolvm pi start --name pi-work
smolvm hermes start --name hermes-work
smolvm openclaw start --name openclaw-work
```

Each command accepts sandbox options such as `--mount`, `--memory`, and `--disk-size`. Add `--no-attach` if you want to start the sandbox without opening the agent session.

## Credentials and configuration

Set the provider key in your host environment before starting the preset. For example:

```bash
export OPENAI_API_KEY=your-key
smolvm codex start --name codex-work
```

Presets copy only the configuration they need where possible. Review what you put in host configuration folders before starting a sandbox, especially when they contain credentials.

## Implementation notes

The command registry is in [`src/smolvm/presets/__init__.py`](../../src/smolvm/presets/__init__.py). Each preset declares its installer, forwarded environment variables, and copied files: [Codex](../../src/smolvm/presets/codex.py), [Claude Code](../../src/smolvm/presets/claude_code.py), [Pi](../../src/smolvm/presets/pi.py), [Hermes](../../src/smolvm/presets/hermes.py), and [OpenClaw](../../src/smolvm/presets/openclaw.py). Preset behavior, including [Hermes coverage](../../tests/test_presets.py#L470-L502), is in [`tests/test_presets.py`](../../tests/test_presets.py).
