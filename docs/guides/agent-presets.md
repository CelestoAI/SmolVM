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
smolvm opencode start --name opencode-work
```

Each command accepts sandbox options such as `--mount`, `--memory`, and `--disk-size`. Add `--no-attach` if you want to start the sandbox without opening the agent session.

OpenCode can also run as a local-only server for the OpenCode app or another client:

```bash
smolvm opencode start --server --port 4096 --name opencode-server
```

SmolVM prints a `http://127.0.0.1:<port>` URL after the server is ready. The guest server listens on its sandbox network, but the host-side forwarding is bound to localhost only. Close the forwarding when it is no longer needed:

```bash
smolvm sandbox port close opencode-server 4096:4096
```

## Credentials and configuration

Set the provider key in your host environment before starting the preset. For example:

```bash
export OPENAI_API_KEY=your-key
smolvm codex start --name codex-work
```

OpenCode supports multiple providers. You can forward common provider keys such as `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`, and `OPENROUTER_API_KEY`, or authenticate from inside the sandbox with `opencode auth login`.

Presets copy only the configuration they need where possible. Review what you put in host configuration folders before starting a sandbox, especially when they contain credentials.

## Implementation notes

The command registry is in [`src/smolvm/presets/__init__.py`](../../src/smolvm/presets/__init__.py). Each preset declares its installer, forwarded environment variables, and copied files: [Codex](../../src/smolvm/presets/codex.py), [Claude Code](../../src/smolvm/presets/claude_code.py), [Pi](../../src/smolvm/presets/pi.py), [Hermes](../../src/smolvm/presets/hermes.py), [OpenClaw](../../src/smolvm/presets/openclaw.py), and [OpenCode](../../src/smolvm/presets/opencode.py). Preset behavior is covered in [`tests/test_presets.py`](../../tests/test_presets.py).
