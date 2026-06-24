# smolvm-core

`smolvm-core` is the Rust package that gives SmolVM faster local VM setup and control.
Most users do not import it directly; they get it automatically when they install `smolvm`.

## What It Speeds Up

`smolvm-core` helps with host-side work that is slow or awkward in Python:

- Linux networking setup for TAP devices, routes, and sysctls.
- Sparse disk image copy and zstd decompression.
- QEMU monitor control through QMP, QEMU's JSON control protocol.
- Firecracker API control through Firecracker's Unix socket API.

The Linux networking helpers still need permission to change host networking directly. If SmolVM lacks that permission, the main package falls back to subprocess commands for networking so the sandbox can still run.

## Check The Installed Wheel

Run this command before debugging native behavior:

```bash
python -m smolvm_core
```

It prints a JSON capability report. A contributor can also inspect capabilities from Python:

```python
from smolvm_core import capabilities

caps = capabilities.detect()
print(caps.as_dict())
```

## Work On The Native Package

When you change `smolvm-core`, rebuild the local extension before judging the behavior:

```bash
uv sync --extra dev
uv sync --reinstall-package smolvm-core
uv run python -m smolvm_core
```

The final command confirms that Python can import the local extension and shows which helpers are available.

For Rust-only validation, run:

```bash
cargo test -p smolvm-core
```

If Rust cannot link against Python, install the Python development package for your interpreter. Some local Python installs also need `LIBRARY_PATH` pointed at the directory that contains the unversioned `libpythonX.Y.so` file.

## Public Python API

Use module imports. Do not import the private compiled extension.

```python
from smolvm_core import disk, firecracker, network, qmp
```

### Networking

Use `network` when setting up Linux TAP networking:

```python
from smolvm_core import network

network.create_tap("tap0", owner_uid=1000)
network.configure_tap("tap0", "172.16.0.1", 32)
network.add_route("172.16.0.2", 32, "tap0")
```

For normal sandbox setup, prefer `prepare_tap()` because it creates the TAP device and configures its address in one native call:

```python
network.prepare_tap("tap0", 1000, "172.16.0.1", 32)
```

### Disk Images

Use `disk` when copying or decompressing raw disk images:

```python
from smolvm_core import disk

method = disk.decompress_zstd_sparse("rootfs.ext4.zst", "rootfs.ext4")
print(method)
```

These helpers preserve sparse zero-filled regions so SmolVM does not waste time writing unused blocks.

### QEMU Control

Use `qmp.QMPClient` for QEMU monitor commands:

```python
from pathlib import Path
from smolvm_core import qmp

with qmp.QMPClient(Path("/tmp/qmp.sock")) as client:
    client.connect()
    print(client.query_status())
```

The main `smolvm.qmp.QMPClient` wraps this core client and converts core errors into `SmolVMError`.

### Firecracker Control

Use `firecracker.FirecrackerClient` for Firecracker API requests:

```python
from pathlib import Path
from smolvm_core import firecracker

client = firecracker.FirecrackerClient(Path("/tmp/firecracker.socket"))
client.wait_for_socket(timeout=10.0)
```

The main `smolvm.api.FirecrackerClient` wraps this core client and keeps SmolVM's public error contract.

## Migrate Old Imports

This refactor intentionally removes the old flat helper imports during the alpha period. Move callers to the public module that owns the operation:

| Old form | New form |
| --- | --- |
| `smolvm_core.has_native_networking()` | `smolvm_core.network.available()` |
| `smolvm_core.has_native_disk_io()` | `smolvm_core.disk.available()` |
| `smolvm_core.has_native_qmp()` | `smolvm_core.qmp.available()` |
| `smolvm_core.has_native_firecracker_api()` | `smolvm_core.firecracker.available()` |
| `smolvm_core.configure_tap(...)` | `smolvm_core.network.configure_tap(...)` |
| `smolvm_core.create_tap(...)`, `delete_tap(...)`, `add_route(...)`, `write_sysctl(...)` | `smolvm_core.network.<function>(...)` |
| `smolvm_core._QmpClient` | `smolvm_core.qmp.QMPClient` |
| raw `_firecracker_*` helpers | `smolvm_core.firecracker.FirecrackerClient` |

## Private Extension Boundary

The compiled module is `smolvm_core._ffi`. Only files inside the `smolvm_core` Python package should import it.

SmolVM production code should use public modules such as `smolvm_core.network` and `smolvm_core.qmp`. Tests enforce that production code does not import `smolvm_core._ffi`, `smolvm_core._smolvm_core`, `_QmpClient`, or raw Firecracker helper functions.

## Rust Layout

The Rust crate is organized like a library first:

- `disk.rs` contains sparse file copy and zstd decompression.
- `network.rs` exposes the public Linux networking facade.
- `route.rs`, `tap.rs`, and `sysctl.rs` contain Linux networking internals.
- `qmp.rs` contains the QEMU monitor client.
- `firecracker.rs` contains the Firecracker socket client.
- `python.rs` registers the private PyO3 extension module.

This keeps the Python binding layer separate from the Rust modules that are useful to test and read on their own.
