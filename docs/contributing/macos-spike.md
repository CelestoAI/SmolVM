# macOS runtime spike

This spike checks whether SmolVM can safely provide a local, disposable macOS desktop through a pinned Lume release. Runtime installation and the machine-readable command surface are verified; a full Apple restore and guest smoke test are still required before release.

## Candidate

- Runtime: Lume 0.4.0 from the Cua repository
- Source tag: `lume-v0.4.0`
- Source commit: `ee15ae942cefe809fd97a565220eca9c6a295ac0`
- License: MIT; see [`THIRD_PARTY_NOTICES.md`](../../THIRD_PARTY_NOTICES.md)
- Host tested: Apple Silicon, APFS
- Installation: signed `lume.tar.gz` app bundle, pinned by SHA-256 in [`src/smolvm/host/lume.py`](../../src/smolvm/host/lume.py)

SmolVM preserves the signed app bundle because Lume needs Apple's virtualization entitlement. `smolvm setup --macos`, `smolvm setup --macos --check-only`, and `smolvm doctor --backend vz` pass on the test host. `codesign --verify --deep --strict` passes after installation, and Gatekeeper reports a notarized Developer ID build.

## Verified interfaces

The pinned CLI supports the operations SmolVM wraps:

- `create --ipsw ... --unattended tahoe --storage ...`
- `get --format json --storage ...`
- `clone --source-storage ... --dest-storage ...`
- `run --no-display --vnc-port 0 --shared-dir PATH:ro`
- `stop --storage ...`
- `delete --force --storage ...`

`get --format json` reports status, the guest IP, SSH readiness, and a VNC URL. Shared folders support `PATH`, `PATH:ro`, and `PATH:rw`; custom mount tags are not supported. Telemetry is disabled in the managed Lume home after installation.

SmolVM lets Lume generate the VNC password in process rather than putting it in SmolVM's long-running Lume command. Runtime output is filtered before it reaches the sandbox log, and SmolVM only returns a password-free loopback endpoint from CLI, Python, and HTTP APIs. Lume still records its active VNC session inside the private VM bundle and may briefly pass it to an SSH child while updating the guest; SmolVM restricts the bundle and known session files to the current host user. This upstream secret path needs explicit acceptance or replacement before a stable release.

## Open release blockers

A complete image build and two-clone smoke test have not run yet. They require about 50 GB and 20–40 minutes on real hardware. The test must verify:

1. Apple IPSW download, installation, and unattended first login.
2. Desktop readiness after cloning, stopping, and restarting.
3. Read-only and writable shared folders in Finder.
4. `.dmg` installation, Gatekeeper, System Integrity Protection, and normal permission prompts.
5. Unique network and guest identities across two concurrent clones.
6. SSH discovery and key-based command execution when SSH becomes ready.
7. APFS copy-on-write behavior and clone latency.
8. Clean deletion of a clone without changing the base image.

The built-in Lume unattended presets create an auto-login account with the fixed credentials `lume` / `lume`. SmolVM does not expose those credentials, but fixed guest credentials do not meet the plan's secure-provisioning requirement. Before release, either rotate the account safely while preserving desktop login, supply a reviewed custom setup path, or replace this part of Lume with a SmolVM-owned helper. This is a release blocker, not a documentation-only follow-up.

## Decision

**Pending / no-go for a stable release.** Continue implementation and tests behind the local macOS preview, but do not describe the feature as production-ready or tag a release until every smoke item passes and fixed guest credentials are removed.
