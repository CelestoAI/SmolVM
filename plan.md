# macOS Desktop Sandboxes — End-to-End Implementation Plan

**Status:** Implementation in progress; stable release blocked on the real-hardware spike and secure guest credential provisioning documented in [`docs/contributing/macos-spike.md`](docs/contributing/macos-spike.md).  
**Initial release:** Experimental local preview  
**Primary user:** A developer on an Apple Silicon Mac who wants a clean macOS desktop, shares an application build, installs it normally, and deletes the sandbox afterward.

## Outcome

A user can run:

```bash
smolvm sandbox create --os macos --name test-mac --mount "$PWD"
smolvm sandbox desktop test-mac
```

SmolVM opens a normal, logged-in macOS desktop. Finder shows the shared folder, the user can double-click a `.dmg`, drag the app into Applications, launch it, and interact with it normally. Deleting the sandbox removes all guest changes while leaving the host unchanged except for SmolVM's local image cache and any explicitly writable shared files.

The feature is a **general disposable Mac desktop**. The runtime, CLI, and API must not contain DMG-specific installation behavior.

---

## Product decisions

1. **Local only.** The preview runs on a Mac the user owns or controls. It does not provide hosted or multi-tenant macOS machines.
2. **Apple Silicon only.** Do not support Intel Macs, Linux hosts, cross-architecture emulation, or non-Apple hardware.
3. **Apple Virtualization framework.** Add a `vz` backend. Do not attempt to boot macOS through the existing QEMU backend.
4. **Lume-backed first implementation.** Put Lume behind a SmolVM-owned driver interface and pin a tested MIT-licensed release. This gets an end-to-end preview working without making Lume's CLI or storage model part of SmolVM's public API. A native `smolvm-vz` helper can replace it later without changing the SDK or CLI.
5. **Local images only.** Download a compatible IPSW from Apple and install it locally. Do not publish, push, save, or load preinstalled macOS images in the preview.
6. **Clean clone per sandbox.** Build one reusable base machine and create an APFS copy-on-write clone for each sandbox.
7. **Desktop is first-class.** Every macOS sandbox starts with a loopback-only VNC display. `smolvm sandbox desktop` opens that display with the host's VNC client. Closing the viewer does not stop the sandbox.
8. **Ordinary macOS security remains enabled.** Do not disable SIP, Gatekeeper, notarization checks, or normal permission prompts.
9. **Shares are general and safe by default.** Map existing `--mount` inputs to VirtioFS. Keep host directories read-only unless `--writable-mounts` is explicit.
10. **Guest commands are deferred.** Shell, exec, file upload/download, and port tunnels remain unavailable until SmolVM can provision secure per-sandbox credentials. Desktop readiness never depends on SSH.
11. **No false feature parity.** Reject unsupported snapshots, restricted internet settings, bridge networking, and vsock with short recovery messages.
12. **Maximum two running macOS guests.** Enforce the limit among SmolVM-managed macOS sandboxes and handle the framework's limit cleanly if another application already consumes a slot.

---

## Preview scope

### Included

- Apple Silicon host preflight
- macOS 14 or newer host for the initial preview; narrow this further after the spike if needed
- One explicitly tested host/guest version matrix at launch
- Latest-compatible Apple IPSW discovery and an explicit local IPSW path
- One-time local base-image installation
- APFS clone-based create/delete
- Start, stop, restart, list, info, and logs
- Loopback-only VNC desktop
- `smolvm sandbox desktop SANDBOX`
- Read-only and explicitly writable VirtioFS shares
- Desktop interaction and Finder-based shared-folder access
- Normal NAT internet access
- CLI, Python SDK, local HTTP API, and dashboard visibility
- Human-readable and JSON progress/errors

### Explicitly excluded

- Hosted or multi-tenant macOS
- Intel Macs or x86 macOS guests
- Published macOS images
- `smolvm image save/load` for macOS images
- Live or durable macOS snapshots
- Pause/resume or saved-memory restore in the first preview
- Domain allow-lists or other egress guarantees
- Bridged networking
- USB, camera, microphone, Bluetooth, Secure Enclave, or hardware passthrough guarantees
- Automated mouse/keyboard control, screenshot assertions, or app-specific test commands
- Automatic TCC permission approval
- Apple ID, signing certificate, or keychain provisioning

---

## User journeys

### First macOS sandbox

```bash
smolvm sandbox create --os macos --name test-mac --mount "$PWD"
```

If no base image exists, interactive output must state the download size estimate, storage requirement, expected installation time, Apple license link, and the local-only nature of the image. Ask for confirmation before downloading. `--yes` skips the prompt. `--json` never prompts; without `--yes`, it returns a structured `confirmation_required` error containing the exact retry command.

Conceptual output:

```text
No local macOS image is ready.

SmolVM will download macOS from Apple and create a reusable local image.
This needs about 50 GB of free space and can take 20–40 minutes.

Continue? [y/N]
```

After installation, SmolVM clones the base, starts the sandbox, and reports:

```text
Sandbox 'test-mac' is running.
Open its desktop with 'smolvm sandbox desktop test-mac'.
```

### Open the desktop

```bash
smolvm sandbox desktop test-mac
```

- If running, open the loopback VNC URL with macOS Screen Sharing.
- If stopped, fail with: `Sandbox 'test-mac' is stopped; run 'smolvm sandbox start test-mac', then 'smolvm sandbox desktop test-mac'.`
- Add `--start` as an explicit convenience to start a stopped sandbox first.
- With `--json`, return the display endpoint and do not launch a host application.

### Bring files into the desktop

Preferred visual path:

```bash
smolvm sandbox create --os macos --name test-mac --mount "$PWD"
```

Finder shows the share under a stable `SmolVM Shared` location. Shell, exec, and file-transfer commands are deferred until secure guest credential provisioning is available.

### Reuse or discard

```bash
smolvm sandbox stop test-mac
smolvm sandbox start test-mac
smolvm sandbox delete test-mac
```

Stop/start preserves the cloned machine. Delete removes the clone and all guest changes.

### Explicit image preparation

Extend the existing image resource rather than adding a macOS-specific top-level command:

```bash
smolvm image build --os macos --ipsw latest -t macos-latest
smolvm image list
smolvm image inspect macos-latest
smolvm image rm macos-latest
```

The current Dockerfile build remains unchanged when `--os macos` is absent. Reject Docker-only flags in macOS mode and reject macOS-only flags in Docker mode.

---

## Architecture

```text
CLI / Python SDK / HTTP API / Dashboard
                  |
             SmolVM facade
                  |
          SmolVMManager lifecycle
                  |
         VzRuntimeAdapter (Python)
                  |
       MacOSRuntimeDriver protocol
                  |
       LumeDriver (pinned executable)
                  |
      Apple Virtualization.framework
```

### New modules

```text
src/smolvm/macos/__init__.py
src/smolvm/macos/models.py       # image manifests, bundle metadata, display endpoint
src/smolvm/macos/images.py       # IPSW/base-image preparation and cache locking
src/smolvm/macos/driver.py       # MacOSRuntimeDriver protocol
src/smolvm/macos/lume.py         # subprocess adapter and JSON parsing
src/smolvm/macos/desktop.py      # loopback endpoint validation and host viewer opening
src/smolvm/runtime/vz.py         # RuntimeAdapter implementation
src/smolvm/host/lume.py          # pinned binary discovery/download/version/SHA checks
```

Keep every Lume command, output parser, and artifact-layout assumption inside `src/smolvm/macos/lume.py`. The rest of SmolVM speaks only SmolVM-owned request/result models.

### Driver interface

Define a narrow protocol with typed inputs and results:

- `probe()`
- `discover_latest_ipsw()`
- `install_base_image(request, progress)`
- `inspect_bundle(path)`
- `clone_bundle(source, destination, instance_identity)`
- `start(bundle, resources, shares, vnc, log_path)`
- `stop(instance, timeout)`
- `status(instance)`
- `get_ip(instance)`
- `delete_bundle(path)`

Do not expose Lume names or raw JSON through public models.

### Process model

- `VzRuntimeAdapter.start()` launches one long-lived backend process per sandbox and tracks its PID through the existing manager.
- Start VNC on a randomly allocated loopback port. Never bind the desktop to `0.0.0.0`.
- Capture backend stdout/stderr in the normal sandbox log file so `smolvm sandbox logs` works.
- Store the backend control socket/path when available.
- Verify display readiness by connecting to the local VNC port; do not require SSH for desktop readiness.
- Closing Screen Sharing must not terminate the backend process.
- `stop()` requests a graceful guest shutdown, waits, then uses the existing forced-process cleanup path.

---

## Data model and compatibility

### Guest and backend

In `src/smolvm/types.py` and `src/smolvm/runtime/backends.py`:

- Add `GuestOS.MACOS = "macos"`.
- Add `BACKEND_VZ = "vz"` and include it in supported backend literals.
- Make automatic backend selection guest-aware:
  - macOS guest + Apple Silicon macOS host -> `vz`
  - Linux guest + macOS host -> keep QEMU/libkrun preference
  - macOS guest on any other host -> fail before image work
- Keep `resolve_backend()` backward compatible for callers that do not pass a guest OS; add a guest-aware resolver used by create paths.

### macOS bundle configuration

Do not overload `rootfs_path`. Add a typed `MacOSMachineConfig` containing at least:

- Base image ID and manifest path
- Per-sandbox bundle path
- Guest OS version and build
- CPU, memory, and logical disk size
- Display width/height and VNC settings
- Local desktop user name
- Share descriptors

Add `macos_machine: MacOSMachineConfig | None` to `VMConfig` and make `rootfs_path` optional. Validators enforce:

- macOS requires `backend="vz"`, `macos_machine`, no kernel/initrd, and the macOS boot mode.
- Non-macOS guests continue to require the existing rootfs/kernel combinations.
- macOS rejects bridge networking, restricted `internet_settings`, and explicit vsock.
- Existing serialized Linux and Windows VM configs continue to load unchanged.

Use a new boot mode such as `"platform"` rather than claiming macOS uses QEMU firmware boot.

### Image manifest

Each base image under `~/.smolvm/images/macos/<name>/` has a versioned `manifest.json` containing:

- Schema version
- Image name
- Guest macOS version/build
- Source IPSW URL and digest when available
- Hardware model metadata
- Required minimum CPU/memory
- Logical and allocated disk sizes
- Creation timestamp
- Driver name/version
- Artifact filenames and digests for small metadata files

Never put account passwords, private SSH keys, VNC credentials, or host paths in the image manifest.

### Instance bundle

Store per-sandbox bundles under the manager's data directory, for example:

```text
~/.smolvm/macos-vms/<vm-id>/
```

A clone must receive a unique machine identifier and MAC address. The spike must prove that the selected Lume clone operation does this correctly; otherwise SmolVM must regenerate them before the implementation proceeds.

### Runtime display state

Add a general `DesktopEndpoint` model:

- `protocol` (`"vnc"` initially)
- `host` (must be loopback)
- `port`
- `viewer_url` (`vnc://127.0.0.1:<port>`)
- Optional width/height

Add `display: DesktopEndpoint | None` to `VMInfo` and `RuntimeLaunch`. Persist it as a nullable JSON `display` column in both SQLite and Postgres `vms` tables. Update:

- `src/smolvm/storage/_protocol.py`
- `src/smolvm/storage/_base.py`
- `src/smolvm/storage/_sqlite.py`
- `src/smolvm/storage/_postgres.py`

Migrations must be additive and old databases must continue to open.

### Managed NAT

Apple's NAT assigns the address after startup. Extend `NetworkConfig` with a backend-managed NAT mode rather than filling in QEMU constants. In this mode:

- Network state may be absent while the VM is created/stopped.
- The adapter discovers and persists the guest IP after startup.
- Gateway/netmask and TAP device are not required.
- Guest IP discovery may be recorded for diagnostics, but public SSH and tunnel operations remain disabled in the preview.

Audit every branch that currently assumes a non-TAP backend is QEMU/libkrun and every direct access to `network.guest_ip`.

### Guest credentials

Shell, exec, file transfer, and port tunnels are deferred until SmolVM can provision secure per-sandbox credentials. The preview does not expose Lume's fixed guest account through those public operations.

Before enabling guest commands, provisioning must:

- Generate per-image or per-instance credentials rather than ship Lume's fixed defaults.
- Install the user's SmolVM public key.
- Restrict SSH to key authentication where compatible with desktop login.
- Keep VNC bound to loopback and use a random VNC password if the selected driver supports it.
- Avoid printing secrets in logs or JSON output.

If secure randomized credentials cannot be achieved with the pinned Lume release, the project must either patch the MIT-licensed helper or move the necessary provisioning code into `smolvm-vz` before enabling guest commands.

---

## Implementation phases

## Phase 0 — Technical spike and release gates

Build an isolated prototype before changing public types.

- [ ] Pin a candidate Lume release, checksum, source commit, and MIT license text.
- [ ] Prove IPSW `latestSupported` discovery on the oldest intended host.
- [ ] Install a base image with unattended setup and record download/install timings.
- [ ] Prove the guest reaches a normal logged-in desktop without manual Setup Assistant steps.
- [ ] Prove VNC binds to loopback, survives viewer disconnect/reconnect, and accepts keyboard/mouse input.
- [ ] Prove APFS clone creation, unique machine identifier, unique MAC address, and independent guest writes.
- [ ] Prove a host directory appears in Finder through VirtioFS in read-only and writable modes.
- [ ] Prove a DMG can be opened from the read-only share and its app copied into guest Applications.
- [ ] Prove stop/start preserves the installed app and delete removes the clone.
- [ ] Record guest IP discovery for diagnostics without exposing SSH operations.
- [ ] Determine how to generate secure credentials before enabling guest commands.
- [ ] Run two macOS guests and capture the exact error from a third.
- [ ] Verify behavior after closing the viewer, host sleep/wake, and an interrupted backend process.
- [ ] Record logical versus allocated storage on default APFS and an external non-APFS data directory.

**Exit criteria:** all core operations work on one declared host/guest matrix; secure credentials have a viable implementation; no upstream command requires scraping unstable human output. If Lume lacks stable machine-readable output or secure provisioning, implement the smallest native Swift helper before continuing.

Deliver the spike findings in `docs/contributing/macos-spike.md`, including the pinned compatibility matrix and rejected approaches.

## Phase 1 — Models, backend selection, and persistence

- [ ] Add macOS guest/backend constants and literals across `types.py`, server models, browser config exclusions, image metadata, and tests.
- [ ] Introduce guest-aware backend resolution without changing Linux-on-macOS defaults.
- [ ] Add `MacOSMachineConfig`, `DesktopEndpoint`, platform boot mode, and conditional `VMConfig` validation.
- [ ] Make rootfs-only manager helpers explicitly reject or skip macOS rather than dereferencing `rootfs_path`.
- [ ] Add managed-NAT state and audit networking assumptions.
- [ ] Add display persistence to SQLite/Postgres with additive migrations.
- [ ] Keep command-channel configuration disabled for macOS until secure credentials are available.
- [ ] Add focused unit tests in `tests/test_types.py`, `tests/test_backends.py`, `tests/test_storage.py`, and a new `tests/test_macos_models.py`.

**Exit criteria:** old persisted VMs round-trip unchanged; macOS configs validate only on supported host/backend combinations; Linux/Windows tests remain green.

## Phase 2 — Host setup and pinned driver

- [ ] Add Lume discovery and version probing under `src/smolvm/host/lume.py`.
- [ ] Decide during the spike whether `smolvm setup --backend vz` downloads a pinned standalone binary or provides a pinned Homebrew command. Prefer a SmolVM-managed checksum-verified binary under `~/.smolvm/bin` if upstream packaging supports it.
- [ ] Include the Lume MIT notice in the source distribution and generated wheel notices when bundling/downloading it.
- [ ] Add `vz_status()`, availability messages, and actionable recovery commands to `runtime/backends.py`.
- [ ] Add `smolvm doctor --backend vz` checks for:
  - Apple Silicon
  - Supported host macOS version
  - Virtualization support/entitlement
  - Pinned driver version
  - APFS image/data storage
  - Available memory and disk space
  - Screen Sharing URL handler
- [ ] Extend `scripts/system-setup-macos.sh` without making QEMU mandatory for users who only request `vz`.
- [ ] Update `tests/test_setup.py`, `tests/test_doctor.py`, and `tests/test_backends.py`.

**Exit criteria:** a fresh supported Mac gets one exact setup path and `doctor` catches every known missing prerequisite before IPSW download.

## Phase 3 — Local macOS image management

- [ ] Implement `MacOSImageManager` with build locks, interrupted-install cleanup, manifest validation, disk-space checks, and progress callbacks.
- [ ] Extend `smolvm image build` with mutually exclusive macOS mode:
  - `--os macos`
  - `--ipsw latest|PATH`
  - `-t/--tag`
  - macOS-appropriate memory/disk options
- [ ] Teach image list/inspect/rm to classify macOS images and show guest version, logical size, allocated size, source, and compatibility.
- [ ] Refuse `image save/load` for macOS with a short explanation and local rebuild command.
- [ ] Refuse image removal while a persisted sandbox references that base.
- [ ] Add first-use preparation to `sandbox create --os macos`, including interactive confirmation and `--yes` behavior.
- [ ] Use `latestSupported` rather than guessing an IPSW URL.
- [ ] Ensure failed/cancelled installation leaves no image that can be mistaken for ready.
- [ ] Add `tests/test_macos_images.py` and extend `tests/test_image_build_cmd.py`, `tests/test_image_cmd.py`, and `tests/test_cli.py` with a fake driver.

**Exit criteria:** image build is resumable or safely restartable, inspection is truthful about disk use, and no macOS image leaves the local machine through SmolVM commands.

## Phase 4 — Runtime lifecycle

- [ ] Implement `MacOSRuntimeDriver` and `LumeDriver` with strict JSON/schema parsing, timeouts, redacted errors, and test injection points.
- [ ] Implement `VzRuntimeAdapter` and register it in `SmolVMManager._runtime_adapter_for_backend()`.
- [ ] Add APFS clone materialization and unique instance identity before persisting create success.
- [ ] Allocate loopback VNC ports without races; release allocations on failed create/delete.
- [ ] Launch the VM headlessly with VNC and configured VirtioFS shares.
- [ ] Mark start ready when VNC responds; record any later guest IP discovery only for diagnostics.
- [ ] Persist PID, display endpoint, dynamic network state, and backend logs.
- [ ] Implement graceful stop, forced cleanup, stale-process reconciliation, restart, and delete.
- [ ] Count running SmolVM macOS guests before launch and return a clear two-guest-limit message.
- [ ] Reject pause/resume and snapshot operations for `vz` in the preview with exact supported alternatives (`stop`, `start`, or delete/recreate).
- [ ] Extend cleanup/prune logic to find stale VZ processes, ports, partial clones, and orphaned bundles without deleting valid base images.
- [ ] Add `tests/test_runtime_vz.py`, `tests/test_vm_macos.py`, and fake-driver lifecycle coverage for sync and async manager paths.

**Exit criteria:** create/start/stop/start/delete is idempotent under retries and process failures, and a viewer disconnect never changes VM lifecycle state.

## Phase 5 — Desktop, shares, and generic guest operations

- [ ] Implement loopback-only endpoint validation in `macos/desktop.py` before invoking any URL opener.
- [ ] Add `SmolVM.open_desktop()` and `SmolVM.desktop_endpoint`; do not change the existing `SmolVM.desktop()` class factory used for Linux desktop sessions.
- [ ] Add `smolvm sandbox desktop SANDBOX [--start] [--json]` to `src/smolvm/cli/commands/app.py` and its handler to `src/smolvm/cli/main.py`.
- [ ] Open `vnc://...` through the host only after checking the sandbox and endpoint state.
- [ ] Map each `WorkspaceMount` to a uniquely named VirtioFS share. Sanitize names and reject collisions.
- [ ] Make shares visible in Finder under a stable `SmolVM Shared` naming convention.
- [ ] Document the Finder volume path as the supported shared-folder location; defer guest-path symlinks until guest commands are enabled.
- [ ] Verify read-only means guest writes fail and writable means changes reach the host.
- [ ] Reject shell, exec, upload/download, and SSH tunnels for macOS with short recovery messages until secure credentials are available.
- [ ] Add a macOS environment-variable implementation only if existing `sandbox env` is advertised for macOS; otherwise reject it in the preview rather than writing Linux `/etc/profile.d` files.
- [ ] Ensure `sandbox info --json` includes a sanitized display endpoint but no credentials or host bundle paths.
- [ ] Add tests in `tests/test_cli.py`, `tests/test_facade.py`, `tests/test_workspace.py`, and new desktop-specific tests.

**Exit criteria:** the primary user journey works without DMG-specific code, and a read-only shared host folder cannot be modified from the guest.

## Phase 6 — HTTP API and dashboard

- [ ] Add `macos` and `vz` to the sanitized API request models in `src/smolvm/server/models.py`.
- [ ] Add a sanitized desktop endpoint to `SandboxResponse` or a dedicated `DesktopResponse`.
- [ ] Add `GET /vms/{id}/desktop` and an optional local-only `POST /vms/{id}/desktop/open`; remote API callers receive the endpoint but must not cause arbitrary URL opening on the server host.
- [ ] Update OpenAPI, regenerate `ts/openapi.json`, and update the TypeScript client.
- [ ] Show macOS and desktop availability on dashboard VM cards.
- [ ] Add an `Open Desktop` action that opens the loopback VNC URL. Do not conflate macOS VMs with `BrowserSessionInfo` or the existing Chromium desktop session model.
- [ ] Add dashboard/server tests and UI tests for running, stopped, missing-endpoint, and unsupported sandboxes.

**Exit criteria:** SDK, CLI, HTTP, and dashboard all report the same lifecycle/display state and never expose secrets.

## Phase 7 — Hardening, real-machine tests, docs, and release

- [ ] Add a dedicated Apple Silicon macOS E2E runner; hosted Linux CI cannot validate this backend.
- [ ] Keep normal unit tests driver-mocked so Linux CI remains fast and complete.
- [ ] Add a real-machine E2E marker and workflow covering:
  - base image already prepared
  - clone/create
  - VNC readiness
  - shared-folder marker visible in the guest
  - Finder access to read-only and writable shares
  - stop/start persistence
  - delete cleanup
  - two-guest limit
- [ ] Add a manual acceptance checklist that opens Finder, mounts a DMG, copies an app into Applications, launches it, closes/reopens Screen Sharing, and confirms host isolation.
- [ ] Add failure-injection tests for interrupted image install, clone failure, VNC port collision, backend crash, stop timeout, stale PID, missing share, non-APFS data directory, and external consumption of VZ slots.
- [ ] Run the full suite: `pytest`, `uv run ruff check .`, `uv run ruff format --check .`, Rust tests, OpenAPI drift, and macOS E2E.
- [ ] Update:
  - `README.md` with one short local desktop example
  - `docs/installation.md`
  - `docs/guides/sandboxes.md`
  - new `docs/guides/macos.md`
  - `docs/reference/cli.md`
  - `docs/contributing/architecture.md`
  - third-party notices and release notes
- [ ] Document supported host/guest versions, resource requirements, two-VM limit, local-only scope, Apple license link, APFS requirement, unsupported features, and how to remove all local macOS data.
- [ ] Label the feature experimental and require an explicit compatibility matrix in release notes.
- [ ] Do not add macOS artifacts to `src/smolvm/images/published.py` or the published-image release workflow.

**Release gate:** the real-machine acceptance journey passes on every declared supported host version, secure credentials are in place, license notices are included, unsupported security controls fail closed, and Linux/Windows behavior is unchanged.

---

## Error and warning requirements

Every new user-facing message follows `AGENTS.md`: plain English, short, true in every state, and includes the exact recovery command.

Required cases include:

- Non-Apple-Silicon host
- Unsupported host macOS version
- Missing/outdated VZ driver
- No compatible IPSW
- Insufficient RAM or disk
- Image storage not on APFS
- First-use confirmation required
- Interrupted or corrupt base image
- Two active macOS guests
- Display endpoint unavailable
- Stopped sandbox on `sandbox desktop`
- Missing shared host folder
- Unsupported bridge/vsock/network restriction/snapshot/pause operation
- Base image still referenced by sandboxes

JSON `error` and `warnings` values must carry the same complete message as human output.

---

## Security and legal checklist

- [ ] Download IPSWs only through Apple's supported discovery URL or an explicit user-supplied local path.
- [ ] Validate HTTPS, download destination, expected file type, and available storage before installation.
- [ ] Verify the pinned driver binary with a committed SHA-256 before execution.
- [ ] Include its MIT license and source/version provenance.
- [ ] Bind VNC and control APIs only to loopback.
- [ ] Validate all ports and URLs before passing them to `open` or clients.
- [ ] Never persist or print plaintext passwords unless an explicit first-login workflow absolutely requires it; if so, store with restrictive permissions and document removal.
- [ ] Keep guest commands disabled until key-only SSH is provisioned; reject unsafe host share paths/symlinks using the same standards as current mounts.
- [ ] Do not silently downgrade a read-only share to writable.
- [ ] Do not claim domain restrictions, snapshots, or hardware isolation features that are not implemented.
- [ ] Link Apple's current macOS software license and state that users must run on Apple hardware they own or control for permitted development/testing use.
- [ ] Obtain legal review before any future image redistribution or hosted service.

---

## Observability

Add structured log events for:

- IPSW discovery/download/install progress
- Base-image lock acquisition and completion
- Clone start/completion and allocated bytes
- VZ backend launch/exit
- VNC readiness and reconnect-safe endpoint
- Guest IP discovery
- Share configuration (paths may be logged only at debug level)
- Graceful versus forced stop
- Cleanup and orphan recovery

Add timing callbacks consistent with existing create/start progress so long first-run preparation never appears hung. Do not add telemetry that sends local image names, host paths, or application details off the machine.

---

## Success metrics for the preview

- At least 95% successful clone/start/desktop-open attempts across 50 repeated local runs on the supported matrix.
- No host file changes through default read-only shares.
- No orphan backend process or instance bundle after failed create/delete tests.
- Viewer can disconnect and reconnect ten times without stopping the VM.
- Stop/start preserves guest-installed applications; delete/recreate returns to the clean base.
- First-run failures always leave either a valid image or a clearly removable partial artifact, never a falsely ready image.
- Existing Linux and Windows test suites have no behavior regressions.

---

## Known risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Lume CLI/output changes | Pin one version and isolate all integration behind `MacOSRuntimeDriver`. |
| Unattended Setup Assistant breaks on a macOS release | Ship only a tested matrix; discover `latestSupported` but reject unverified guest versions until validated. |
| Fixed/default guest credentials | Treat randomized provisioning as a release blocker; patch or replace the helper if needed. |
| VNC endpoint exposes the desktop | Bind only to loopback, use random authentication when supported, and validate endpoints before opening. |
| APFS clone semantics unavailable on external storage | Preflight filesystem type and fail with a command using the default SmolVM data directory; consider full-copy fallback only later. |
| User expects Firecracker-like boot density | Document full-macOS resource and startup characteristics before download. |
| Apple enforces two active guests across applications | Enforce SmolVM's own count and translate framework errors when external VMs use a slot. |
| Existing rootfs assumptions cause regressions | Add conditional typed macOS config, audit each rootfs access, and retain old serialized defaults/tests. |
| Dashboard desktop model gets confused with browser sessions | Keep VM desktop endpoints on `VMInfo`; do not store them in `BrowserSessionInfo`. |
| Apple licensing blocks future hosted plans | Keep preview local-only and require separate legal/product review for hosting. |

---

## Deferred follow-up roadmap

Only after the local preview is stable:

1. Replace Lume with a signed/notarized `smolvm-vz` helper if upstream coupling or credential provisioning remains problematic.
2. Add host-side noVNC for a browser viewer and richer dashboard integration.
3. Add screenshots and generic keyboard/mouse automation as a separate computer-use API.
4. Add disk-only stopped snapshots after defining multi-artifact atomicity and restore semantics.
5. Evaluate Apple saved-state suspend/resume separately from durable snapshots.
6. Evaluate multiple named macOS base images and version pinning.
7. Evaluate safe writable-share workflows and explicit copy-in/copy-out UX.
8. Consider hosted macOS only as a separate legal and infrastructure project.

---

## Definition of done

The implementation is complete when a developer with a supported Apple Silicon Mac can prepare a local macOS image, create an isolated sandbox clone, open and reconnect to its desktop, access an explicitly shared folder in Finder, install and run an application through ordinary macOS interactions, stop/restart the sandbox, delete it, and create a clean replacement—without DMG-specific product behavior, unintended writes to the host, exposed desktop ports, leaked credentials, or regressions to existing SmolVM backends.
