# CLI reference

The CLI creates and manages disposable sandboxes. Run `smolvm COMMAND --help` for the current options on your installed version; this page helps you choose the right command.

## Prepare the host

| Command | Use it to |
| --- | --- |
| `smolvm setup` | Install or check local runtime dependencies. |
| `smolvm doctor` | Check whether this machine can run sandboxes. |
| `smolvm bridge check BRIDGE` | Check an existing Linux bridge before connecting a sandbox to it. |
| `smolvm update` | Upgrade to the latest stable release. |
| `smolvm prune` | Remove stale cached images. |

## Work with sandboxes

Run these in the order you need them:

| Command | Use it to |
| --- | --- |
| `smolvm sandbox create` | Create a sandbox. Add `--network bridge --bridge BRIDGE` only when the sandbox should appear as a separate computer on that network. |
| `smolvm sandbox list` / `info` | Find or inspect sandboxes. |
| `smolvm sandbox shell` / `ssh` | Open a shell. `shell` uses SmolVM's fast control channel when available; `ssh` explicitly uses SSH. |
| `smolvm sandbox start` / `stop` | Start or stop a sandbox. |
| `smolvm sandbox pause` / `resume` | Temporarily freeze and continue a running sandbox. |
| `smolvm sandbox delete` | Remove one or more sandboxes. |

### Sandbox data and connections

| Command | Use it to |
| --- | --- |
| `smolvm sandbox file upload` / `download` | Copy a file in or out. |
| `smolvm sandbox env set` / `unset` / `list` | Manage persistent environment variables. |
| `smolvm sandbox port expose` / `close` / `list` | Manage local port forwarding. |
| `smolvm sandbox snapshot create` / `restore` / `list` / `delete` | Save and restore supported sandbox state. |

## Start a prepared agent

`smolvm codex start`, `smolvm claude start`, `smolvm pi start`, `smolvm hermes start`, and `smolvm openclaw start` create a sandbox and install that agent. See [Agent presets](../guides/agent-presets.md).

## Browser, local services, and Windows

| Command | Use it to |
| --- | --- |
| `smolvm browser start` / `open` / `list` / `logs` / `stop` | Manage browser sandboxes. |
| `smolvm ui` | Start the local dashboard. |
| `smolvm server start` | Start the local HTTP API. |
| `smolvm windows build-image` | Build a Windows qcow2 image. |

## Common options

`--json` is available on commands that return structured output. `--backend` selects `auto`, `firecracker`, `qemu`, or `libkrun` where the command supports a runtime choice. `--boot-timeout` controls how long an operation waits for a ready sandbox.

**Implementation notes:** the command definitions are the source of truth in [`src/smolvm/cli/commands/app.py`](../../src/smolvm/cli/commands/app.py), including available flags and help text. The CLI command surface is tested by [`tests/test_cli.py`](../../tests/test_cli.py).
