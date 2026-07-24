# CLI reference

The CLI creates and manages disposable sandboxes. Run `smolvm COMMAND --help` for the current options on your installed version; this page helps you choose the right command.

## Prepare the host

| Command | Use it to |
| --- | --- |
| `smolvm setup` | Install or check local runtime dependencies. Add `--macos` to prepare the macOS desktop runtime. |
| `smolvm doctor` | Check whether this machine can run sandboxes. Use `--backend vz` to check macOS desktop support. |
| `smolvm bridge check BRIDGE` | Check an existing Linux bridge before connecting a sandbox to it. |
| `smolvm update` | Upgrade to the latest stable release. |
| `smolvm prune` | Remove stale cached images (alias for `smolvm image prune`). |

## Work with sandboxes

Run these in the order you need them:

| Command | Use it to |
| --- | --- |
| `smolvm sandbox create` | Create a sandbox. Add `--network bridge --bridge BRIDGE` only when the sandbox should appear as a separate computer on that network. |
| `smolvm sandbox list` / `info` | Find or inspect sandboxes. |
| `smolvm sandbox shell` / `ssh` | Open a shell. `shell` uses SmolVM's fast control channel when available; `ssh` explicitly uses SSH. |
| `smolvm sandbox desktop` | Open a running macOS sandbox in Screen Sharing. Add `--start` to start it first. |
| `smolvm sandbox exec` | Run one command inside a running sandbox and print its output — handy for scripts and agents. Put the command after `--`, e.g. `smolvm sandbox exec my-sandbox -- ls -la`. Add `--start` to start the sandbox first if it isn't running. |
| `smolvm sandbox logs` | Show a sandbox's boot and console logs. Add `--follow` to keep printing new lines. |
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

## Manage downloaded images

The first time you start a sandbox or agent, SmolVM downloads the files it boots from and keeps them on disk so later starts are fast. These commands manage that storage, and they work like Docker's image commands if you know those:

| Command | Use it to |
| --- | --- |
| `smolvm image pull <preset>` | Download an image ahead of time, for example before going offline. |
| `smolvm image pull --all` | Download every image available for this machine in one go. |
| `smolvm images` (or `image list` / `image ls`) | See which images are downloaded, when, and how much space they use. |
| `smolvm image inspect <name>` | See one image in detail: files, checksums, and where it came from. |
| `smolvm image build -t NAME .` | Build a custom image from a Dockerfile (needs Docker installed). |
| `smolvm image build --os macos --ipsw latest -t NAME` | Prepare a reusable macOS image locally from an Apple restore file. |
| `smolvm image save <name> -o FILE` / `image load -i FILE` | Copy an image to a machine without internet access. |
| `smolvm image rm <name>` | Remove a downloaded image to free disk space. |
| `smolvm image prune` | Remove images left behind by older SmolVM versions. |

Images are stored in `~/.smolvm/images`. To keep them somewhere else, set the `SMOLVM_IMAGE_DIR` environment variable — sandboxes read it too, so images you pull are found when a sandbox starts. The `--image-dir` option points a single `smolvm image` command at a different folder; sandboxes do not read that folder.

## Browser, local services, and Windows

| Command | Use it to |
| --- | --- |
| `smolvm browser start` / `open` / `list` / `logs` / `stop` | Manage browser sandboxes. |
| `smolvm ui` | Start the local dashboard. |
| `smolvm server start` | Start the local HTTP API. |
| `smolvm windows build-image` | Build a Windows qcow2 image. |

## Shell completion

Turn on tab completion so your shell can finish `smolvm` commands, options, and the names of your existing sandboxes as you type. One command sets it up:

```bash
smolvm completion bash --install   # also works with: zsh, fish
```

Open a new shell afterward, then type `smolvm sandbox ssh` followed by a space and press Tab to complete a sandbox name.

Prefer to wire it up yourself? Run the same command without `--install` to print the script, then load it your own way:

```bash
# bash — add to ~/.bashrc
eval "$(smolvm completion bash)"

# zsh — add to ~/.zshrc
eval "$(smolvm completion zsh)"

# fish — create the folder once, then write the completion file
mkdir -p ~/.config/fish/completions
smolvm completion fish > ~/.config/fish/completions/smolvm.fish
```

## Common options

`--json` is available on commands that return structured output. `--backend` selects `auto`, `firecracker`, `qemu`, `libkrun`, or `vz` where the command supports that runtime. The `vz` choice is only for macOS guests on Apple Silicon. `--boot-timeout` controls how long an operation waits for a ready sandbox.

**Implementation notes:** the command definitions are the source of truth in [`src/smolvm/cli/commands/app.py`](../../src/smolvm/cli/commands/app.py), including available flags and help text. The CLI command surface is tested by [`tests/test_cli.py`](../../tests/test_cli.py).
