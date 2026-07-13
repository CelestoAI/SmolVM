# Architecture

SmolVM turns a requested sandbox into a guest image, a runtime process, and a small amount of local state. This map helps contributors find the code that owns a behavior before changing it.

## Main layers

| Layer | Responsibility | Code |
| --- | --- | --- |
| Public API | Instance-oriented `SmolVM` lifecycle, commands, files, mounts, ports, and snapshots. | [`src/smolvm/facade.py`](../../src/smolvm/facade.py) |
| Lifecycle manager | Persists state and creates, starts, stops, deletes, and restores sandbox resources. | [`src/smolvm/vm.py`](../../src/smolvm/vm.py) |
| Runtime adapters | Start QEMU, Firecracker, or libkrun and handle backend-specific snapshot work. | [`src/smolvm/runtime/`](../../src/smolvm/runtime) |
| Guest communication | Chooses and implements SSH or vsock control. | [`src/smolvm/comm/`](../../src/smolvm/comm) |
| Images | Finds, builds, caches, and prepares boot images. | [`src/smolvm/images/`](../../src/smolvm/images) |
| Host services | Checks the host and manages networking, disks, and setup. | [`src/smolvm/host/`](../../src/smolvm/host) |
| State | Stores sandbox, snapshot, and browser-session metadata in SQLite or Postgres. | [`src/smolvm/storage/`](../../src/smolvm/storage) |
| Interfaces | Registers the CLI, browser sessions, dashboard, and HTTP API. | [`src/smolvm/cli/`](../../src/smolvm/cli), [`src/smolvm/browser.py`](../../src/smolvm/browser.py), [`src/smolvm/server/`](../../src/smolvm/server) |

## A normal sandbox lifecycle

1. The CLI or Python API creates a `VMConfig`.
2. `SmolVMManager` materializes an isolated disk and records sandbox state.
3. The selected runtime starts the guest.
4. `SmolVM` waits for the selected control channel, then performs commands or file operations.
5. Stop or delete releases runtime resources; delete also removes managed state unless the configuration says otherwise.

This flow is implemented primarily by [`SmolVM`](../../src/smolvm/facade.py) and [`SmolVMManager`](../../src/smolvm/vm.py), and exercised by [`tests/test_facade.py`](../../tests/test_facade.py), [`tests/test_vm.py`](../../tests/test_vm.py), and [`tests/test_async_lifecycle.py`](../../tests/test_async_lifecycle.py).

## Backend and control-channel selection

`auto` chooses QEMU on macOS and Firecracker elsewhere. Automatic control-channel selection uses supported vsock when available and SSH otherwise; Windows guests use SSH in automatic mode. An explicit vsock request for Windows is rejected with `VsockNotSupportedError` instead of falling back to SSH. These are current implementation details, so update this document with [`runtime/backends.py`](../../src/smolvm/runtime/backends.py) and [`comm/select.py`](../../src/smolvm/comm/select.py) whenever selection rules change.

## How to change behavior safely

Start from the public API or CLI test that describes the user-visible outcome. Then follow the call into the owning layer above. Add or update a focused test in `tests/` with the code change, and update the relevant user guide if the observable behavior changes.
