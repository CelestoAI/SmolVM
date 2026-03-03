# Vsock Control Plane Implementation Plan

This document proposes an implementation plan for adding a **vsock-based control plane** to SmolVM for automated in-guest command execution, streaming logs, health/metadata, and optional artifact transfer.

## 1) Current codebase fit

### Existing components to build on
- `SmolVMManager.start()` already configures Firecracker boot source, machine config, rootfs drive, and one NIC through `FirecrackerClient`. The vsock device should be wired in this same startup path for Firecracker-backed VMs.  
- `FirecrackerClient` in `src/smolvm/api.py` provides clear, typed wrappers around API resources (`/boot-source`, `/machine-config`, `/drives`, `/network-interfaces`, `/actions`). Add a sibling method for vsock devices.  
- `SmolVM` facade in `src/smolvm/facade.py` exposes the user-facing execution API (`run`) and is the right place to add transport selection (`ssh` vs `vsock`) while preserving backwards compatibility.  
- `ImageBuilder` in `src/smolvm/build.py` already assembles guest images and installs `/init`; this is where the in-guest agent binary/service can be included and started.  
- Pydantic types in `src/smolvm/types.py` (`VMConfig`, `VMInfo`, `CommandResult`) are central extension points for vsock config, per-VM auth secret metadata references, and richer command result payloads.  

### Gaps today
- No Firecracker API wrapper for adding a vsock device.
- No in-guest control-plane daemon.
- No host-side vsock protocol/client.
- Execution path is SSH-centric and assumes guest networking.

## 2) Scope and rollout strategy

### Phase A (MVP, required)
1. Firecracker vsock device provisioning.
2. In-guest `vsock-agent` with auth + command execution + hard limits.
3. Host-side `vsock-client` supporting:
   - `exec` (request/response)
   - `stream` (incremental stdout/stderr)
   - `cancel`
   - `health`, `system_info`, `shutdown`
4. Facade integration (`SmolVM.run(..., transport="vsock")` or automatic fallback preference).

### Phase B (recommended)
1. File transfer (`put_file`, `get_file`) with size caps and checksum validation.
2. Job registry persistence in guest for reconnect/retry semantics.
3. CLI subcommands for explicit vsock operations.

### Phase C (hardening)
1. End-to-end metrics + tracing.
2. Protocol/version negotiation.
3. Strict compatibility tests across kernel/image variants.

## 3) Detailed implementation plan

### Step 1 — Data model and state extensions
**Files:** `src/smolvm/types.py`, `src/smolvm/storage.py` (if schema migration needed), `src/smolvm/vm.py`

- Extend `VMConfig` with optional `vsock` config:
  - `enabled: bool = True` (for Firecracker)
  - `guest_port: int = 5000`
  - `uds_path: Path | None` (host-side Firecracker vsock UDS; default under data dir)
- Extend runtime info with resolved vsock connection details (e.g., host UDS path + guest port).
- Generate/store a **per-VM secret/token** at VM creation time; store in host state DB and inject into guest image/bootstrap location with strict permissions.

**Acceptance criteria:**
- Creating a VM produces deterministic vsock metadata for Firecracker VMs.
- Token material is never logged in plaintext.

### Step 2 — Firecracker API support for vsock
**Files:** `src/smolvm/api.py`, `src/smolvm/vm.py`, `tests/test_api.py` (new or extend relevant tests)

- Add `FirecrackerClient.add_vsock(vsock_id: str, guest_cid: int, uds_path: Path)` wrapper targeting Firecracker `/vsock` resource.
- In `SmolVMManager.start()`, after machine config/drive/network setup and before `start_instance()`, call `add_vsock(...)` for Firecracker backend.
- Derive stable `guest_cid` from VM identity (collision-resistant within host) and keep mapping in state.

**Acceptance criteria:**
- Firecracker boot path invokes vsock setup exactly once.
- Startup fails fast with actionable error if vsock setup fails.

### Step 3 — Guest agent packaging and boot integration
**Files:** `src/smolvm/build.py`, new guest assets directory (e.g., `src/smolvm/guest_assets/`), relevant tests

- Package a small `vsock-agent` in the guest image build flow:
  - Start as non-root user.
  - Bind fixed guest vsock port (default 5000).
  - Read token from root-owned file with minimal permissions.
- Update generated `/init` logic (or systemd unit when applicable) to launch and supervise the agent.
- Ensure agent startup does not block SSH startup for compatibility.

**Acceptance criteria:**
- Fresh image boots with both SSH (legacy) and agent (new) available.
- Agent process restarts or fails clearly in logs.

### Step 4 — Protocol definition and host client
**Files:** new module(s) `src/smolvm/vsock_protocol.py`, `src/smolvm/vsock_client.py`, tests

- Define a framed protocol (length-prefixed JSON envelopes + binary chunks where needed):
  - `auth`, `exec_start`, `exec_stream`, `exec_result`, `cancel`, `health`, `system_info`, `shutdown`, `put_file`, `get_file`.
- Add protocol version field and explicit error codes.
- Implement host client with:
  - Connection/retry handling.
  - Request timeout and max message/body constraints.
  - Incremental streaming callback/iterator API.

**Acceptance criteria:**
- Client can execute short and long-running commands through vsock.
- Stream ordering for stdout/stderr is preserved by sequence numbers.

### Step 5 — Guest agent command execution + policy controls
**Files:** guest agent source, tests (unit + integration)

- Enforce security controls per request:
  - Required auth token on every call.
  - Max runtime, max stdout/stderr bytes, max concurrent jobs.
  - Optional cwd/env allowlist policy.
- Implement job registry:
  - `job_id` lifecycle (`queued/running/completed/failed/cancelled`).
  - Cancellation via process group signaling.
- Return structured result: `exit_code`, `stdout`, `stderr`, `duration_ms`, truncation flags.

**Acceptance criteria:**
- Invalid token is denied consistently.
- Limits are enforced and surfaced as structured errors.

### Step 6 — Facade + CLI integration
**Files:** `src/smolvm/facade.py`, `src/smolvm/cli.py`, `src/smolvm/__init__.py`, docs/tests

- Add facade methods:
  - `run_vsock(...)`
  - `stream_vsock(...)`
  - Optional `put_file/get_file`.
- Update `run()` default policy:
  - Prefer vsock when agent is healthy.
  - Optional fallback to SSH (feature flag/configurable).
- Add CLI commands (examples):
  - `smolvm exec --transport vsock ...`
  - `smolvm stream ...`
  - `smolvm cp put|get ...`

**Acceptance criteria:**
- Existing SSH flows remain backward compatible.
- New commands documented and discoverable in `--help`.

### Step 7 — Testing matrix and CI gating
**Files:** `tests/test_vm.py`, `tests/test_facade.py`, new vsock-specific tests

- Unit tests:
  - Firecracker vsock API payload correctness.
  - Protocol encode/decode and error handling.
- Integration tests:
  - End-to-end exec over vsock on Firecracker.
  - Streaming + cancel behavior.
  - Token auth rejection.
  - Output/file size cap enforcement.
- Regression tests:
  - SSH execution remains functional.

**Acceptance criteria:**
- `pytest` passes on existing and new tests.
- CI includes at least one vsock-enabled integration lane (can be optional initially, required before GA).

## 4) Security and reliability checklist

- Per-VM token generated on host, rotated on VM recreation.
- No unauthenticated endpoint exposed on guest vsock port.
- Agent runs non-root and uses bounded subprocess execution.
- Bounded memory usage for stream buffering.
- Backpressure-aware streaming to avoid host or guest OOM.
- Protocol versioning and feature negotiation to support rolling upgrades.

## 5) Suggested work breakdown (tickets)

1. **Core plumbing**: Firecracker vsock API + VM state wiring.
2. **Protocol MVP**: framed messages + `health` + `exec` + result.
3. **Guest agent MVP**: auth + execute + limits.
4. **Facade integration**: `run_vsock` and transport selection.
5. **Streaming and cancel**.
6. **File transfer**.
7. **Hardening & observability**.

## 6) Risks and mitigations

- **Risk:** CID collisions across concurrent VMs.  
  **Mitigation:** deterministic allocation + state-backed reservation + collision retry.
- **Risk:** Agent availability race at boot.  
  **Mitigation:** client-side health probe with bounded retries before first exec.
- **Risk:** Protocol bloat and compatibility drift.  
  **Mitigation:** strict schema + version field + compatibility tests.
- **Risk:** Security regressions from fallback behavior.  
  **Mitigation:** explicit config to disable SSH fallback in hardened environments.

## 7) Definition of done (feature level)

- Firecracker-backed VM can execute commands and stream output via vsock with per-request auth and enforced limits.
- Host SDK/facade exposes stable APIs for exec, stream, cancel, health, and shutdown.
- Optional artifact transfer is available with bounded sizes and integrity checks.
- Documentation includes migration guidance and transport selection behavior.
