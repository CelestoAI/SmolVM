# Refactor plan: make SQLite CLI-only

## Target outcome

- `SmolVM()` and the HTTP API never create, open, or import SQLite.
- A `SmolVM` object owns its current `VMInfo` and runtime handles.
- Live host resources are allocated from authoritative OS/runtime state.
- The CLI keeps `smolvm.db` for cross-command discovery and reconnecting.
- Existing CLI databases remain readable.
- The HTTP API lists only sandboxes owned by that API process.

## Status

Implemented in this change. The existing state-manager protocol remains as a
small migration seam inside the low-level manager, but its SDK/API implementation
is now process-local and has no database dependency. This avoids a risky rewrite
of the large sync/async lifecycle engine while enforcing the product boundary.

## Architecture

### Core SDK

`SmolVM` owns `VMInfo`, the runtime process/control handle, transient resource reservations, and snapshot manifests. Core lifecycle operations work on live VM information rather than treating a database row as the source of truth.

### CLI

A small `CLIService` combines the core SDK with a SQLite-backed `CLIRegistry`. The registry records the latest `VMInfo` after each operation. It is inventory, not the authority for whether a process, port, IP, TAP interface, or vsock CID is live.

### HTTP API

The FastAPI process keeps `dict[str, SmolVM]` as its complete inventory. It does not discover or reconnect to CLI-owned sandboxes through SQLite.

## Implementation phases

### 1. Establish the boundary

- Add regression tests proving SDK and API use do not create `smolvm.db`.
- Prove importing `smolvm` does not import the CLI SQLite implementation.
- Preserve CLI create/list/start/stop/delete behavior across separate invocations.
- Preserve compatibility with the existing database path and schema.

### 2. Separate live resource allocation from persistence

- Use process-local resource tracking for SDK/API objects.
- Use advisory resource locks to coordinate independent local SDK processes.
- Check live ports, interfaces, and hypervisor processes before allocation.
- Keep successful allocations in `VMInfo` and make release idempotent.

### 3. Make the high-level lifecycle handle-based

- Keep `SmolVM._info` as the high-level SDK source of truth.
- Require an explicit inventory for reconnect-by-ID integrations.
- Keep the transient state protocol inside `SmolVMManager` as a migration seam,
  rather than rewriting the sync and async lifecycle engines in one risky change.

### 4. Move persistence into the CLI

- Put the SQLite implementation in `src/smolvm/cli/_sqlite.py`.
- Add `src/smolvm/cli/state.py` as the CLI inventory entry point.
- Add `CLIService` as the composition root for persistent CLI handles.
- Keep `smolvm.db` and the current schema for compatibility.

### 5. Remove API persistence

- Resolve API sandbox IDs only from the process registry.
- Implement API listing from that registry.
- Return 404 for unknown IDs rather than reconnecting through host SQLite state.
- Clean up API-owned sandboxes during graceful server shutdown.

### 6. Move remaining metadata out of the core database

- Store snapshot metadata in an atomic `manifest.json` beside snapshot artifacts.
- Make browser SDK objects own their session information in memory.
- Keep browser listing/reconnect persistence only in the CLI registry.

### 7. Delete obsolete infrastructure

- Remove SQLite, the generic state-manager factory, and PostgreSQL state selection from core.
- Remove `SMOLVM_DATABASE_URL` from SDK behavior.
- Keep the low-level manager export for compatibility; reconsider it in a future major release.
- Update SDK documentation and examples that reconnect by ID.

## Validation gates

- `pytest`
- `uv run ruff check .`
- SDK and API create no database.
- Existing CLI databases remain usable.
- CLI lifecycle works across processes.
- Concurrent SDK allocation does not collide.
- Failed create/start operations release resources.
- API graceful shutdown cleans up owned VMs.
- Sync and async lifecycle behavior remains equivalent.
- Snapshot restore works without SQLite.

## Delivery order

1. Transient core state and explicit CLI persistence.
2. Host-backed resource allocation and handle-based lifecycle.
3. CLI registry/service extraction.
4. API/browser cleanup, snapshot manifests, and legacy storage deletion.

Do not remove shared persistent allocation until host-backed allocation passes concurrency tests.
