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

## Open OpenClaw's dashboard

SmolVM installs OpenClaw 2026.9.1 with a supported Node.js 24 runtime. Start it without attaching to the terminal:

```bash
smolvm openclaw start --name openclaw-work --no-attach
```

Then open its private dashboard through a localhost-only connection:

```bash
smolvm openclaw open openclaw-work
```

SmolVM starts the OpenClaw gateway if needed, creates a local port forward, and opens the one-time dashboard link in your browser. If you are working on a remote or headless machine, print the link instead:

```bash
smolvm openclaw open openclaw-work --no-browser
```

Close the local connection with the exact command printed by `openclaw open`. You can also list active connections with `smolvm sandbox port list openclaw-work`.

OpenClaw currently supports Ubuntu sandboxes in SmolVM. Its first start installs the pinned release after boot because the published OpenClaw image predates 2.0; SmolVM will use a prebuilt image again after replacement image artifacts are released and pinned.

## Credentials and configuration

Set the provider key in your host environment before starting the preset. For example:

```bash
export OPENAI_API_KEY=your-key
smolvm codex start --name codex-work
```

OpenCode supports multiple providers. You can forward common provider keys such as `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`, `GEMINI_API_KEY`, and `OPENROUTER_API_KEY`, or authenticate from inside the sandbox with `opencode auth login`.

Presets copy only the configuration they need where possible. Review what you put in host configuration folders before starting a sandbox, especially when they contain credentials.

For OpenClaw, SmolVM copies `~/.openclaw/openclaw.json` and `~/.openclaw/.env` when they exist. It does not copy the whole state directory because OpenClaw 2.0 keeps device and session state in SQLite databases that should not be duplicated into a disposable sandbox. SmolVM also forwards `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `OPENCLAW_GATEWAY_TOKEN`, and `OPENCLAW_GATEWAY_PASSWORD` when set.

If OpenClaw reports a configuration problem, enter the sandbox and inspect it before applying repairs:

```bash
smolvm sandbox shell openclaw-work
openclaw doctor
```

Review `doctor` output before running its repair mode. OpenClaw can follow workspace or storage paths named in copied configuration, so a repair may change data outside `~/.openclaw`.

## Implementation notes

The command registry is in [`src/smolvm/presets/__init__.py`](../../src/smolvm/presets/__init__.py). Each preset declares its installer, forwarded environment variables, and copied files: [Codex](../../src/smolvm/presets/codex.py), [Claude Code](../../src/smolvm/presets/claude_code.py), [Pi](../../src/smolvm/presets/pi.py), [Hermes](../../src/smolvm/presets/hermes.py), [OpenClaw](../../src/smolvm/presets/openclaw.py), and [OpenCode](../../src/smolvm/presets/opencode.py). Preset behavior is covered in [`tests/test_presets.py`](../../tests/test_presets.py).
