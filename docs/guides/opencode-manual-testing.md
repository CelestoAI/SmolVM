# OpenCode Manual Testing Checklist

Use this checklist to validate the stable OpenCode preset on a machine that can boot a SmolVM sandbox. Record the host OS, architecture, backend, OpenCode version, and sandbox name with the test results.

## Test record

The configuration and credentials checks were run on 2026-08-22 from the source checkout on macOS arm64. The test sandbox was `opencode-config-test-20260822`, running Ubuntu with OpenCode `1.18.21`. The sandbox was deleted after the checks.

An ARM64 Alpine/musl runtime smoke test also passed manually: OpenCode `1.18.21` executed `uname -a` and `printf 'ALPINE_OK\\n'` successfully inside `opencode-alpine-test`.

The source fallback is now Alpine-aware and the published-image workflow builds
and smoke-tests `opencode-<arch>-alpine-rootfs.ext4.zst`. The prebuilt checks
below remain pending until those release assets are published and pinned in the
manifest.

## Preparation

- [x] Confirm the CLI is running from the feature branch:

  ```bash
  uv run smolvm --version
  ```

- [x] Confirm the command is registered:

  ```bash
  uv run smolvm opencode start --help
  ```

- [x] Start without provider credentials for the configuration and filtering checks. A real provider key was not used.

## Interactive TUI Startup

- [ ] Start OpenCode in interactive mode:

  ```bash
  uv run smolvm opencode start --name opencode-tui
  ```

- [ ] Confirm the sandbox boots successfully and the OpenCode launch prompt appears.
- [ ] Launch the TUI and confirm the `opencode` command starts.
- [ ] Confirm OpenCode can read the mounted workspace.
- [ ] Run a harmless request, such as asking OpenCode to list the repository files.
- [ ] Confirm provider authentication works through the forwarded API key or `opencode auth login`.
- [ ] Exit OpenCode and confirm the sandbox remains available.

## No-Attach Startup

- [x] Start without opening an interactive session:

  ```bash
  uv run smolvm opencode start --name opencode-config-test-20260822 --no-attach
  ```

- [x] Confirm the command exits successfully.
- [x] Connect manually over SSH:

  ```bash
  uv run smolvm sandbox ssh opencode-config-test-20260822
  opencode --version
  ```

## Configuration and Credentials

- [x] Confirm OpenCode configuration is copied from `~/.config/opencode` to `/root/.config/opencode`. The directory and files were present in the guest.
- [x] Confirm OpenCode auth is copied from `~/.local/share/opencode/auth.json` to `/root/.local/share/opencode/auth.json`.
- [x] Confirm unrelated host environment variables are not forwarded. `UNRELATED_SMOLVM_TEST` was absent in the guest.
- [x] Start without provider keys and confirm the output gives an actionable `opencode auth login` hint.
- [x] Confirm the auth file remains private inside the guest. Its guest mode was `600`.

## Prebuilt Image

Run this section after the OpenCode image assets have been published and added to the manifest.

- [ ] Pull the image:

  ```bash
  uv run smolvm image pull opencode
  ```

- [ ] Confirm the image is cached for the current architecture and backend.
- [ ] Start OpenCode and confirm startup does not run the npm install step.
- [ ] Confirm the preinstalled binary works:

  ```bash
  uv run smolvm sandbox exec opencode-prebuilt -- opencode --version
  ```

- [ ] Repeat on both `amd64` and `arm64` environments when available.
- [ ] Confirm normal TUI mode works from the prebuilt image.

## Alpine Runtime Smoke Test

- [x] Create an ARM64 Alpine sandbox with at least 2 GiB memory and 4 GiB disk.
- [x] Install `nodejs`, `npm`, `git`, `bash`, `ripgrep`, `libgcc`, and `libstdc++`.
- [x] Install `opencode-ai` from npm.
- [x] Confirm `opencode --version` reports `1.18.21`.
- [x] Run a shell-command smoke test and confirm `ALPINE_OK` plus the Alpine guest kernel output.
- [x] Launch the OpenCode TUI on Alpine and receive a successful agent response.
- [ ] Run an authenticated OpenAI request.

## Cleanup
- [x] Delete all sandboxes created by this test:

  ```bash
  uv run smolvm sandbox delete opencode-config-test-20260822 opencode-env-test-20260822
  ```

- [x] Confirm the two temporary test sandboxes were deleted successfully.

## Automated Checks

- [x] Run the focused tests:

  ```bash
  uv run pytest tests/test_presets.py tests/test_cli.py -q -k 'not completion_install'
  uv run pytest tests/test_guest_agent_image.py tests/test_published_images.py -q
  ```

- [x] Run linting on the affected files:

  ```bash
  uv run ruff check src/smolvm/cli/commands/app.py src/smolvm/cli/main.py src/smolvm/presets/opencode.py tests/test_cli.py tests/test_presets.py
  ```
