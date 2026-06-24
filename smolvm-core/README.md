# smolvm-core

`smolvm-core` is the Rust helper package that lets SmolVM do local VM setup faster.
Most users install `smolvm`; it pulls in the matching `smolvm-core` wheel automatically.

```bash
pip install smolvm
```

Install `smolvm-core` directly only when you are developing the native helper package or testing a package release.

## Work On smolvm-core From Source

Use the source checkout when you are changing the Rust helpers or their Python wrappers:

```bash
uv sync --extra dev
uv sync --reinstall-package smolvm-core
uv run python -m smolvm_core
```

The last command should print the capability report for the extension you just built. Run it again after changing Rust or wrapper code so you know Python is loading the current local build.

For Rust-only checks, run:

```bash
cargo test -p smolvm-core
```

If that command cannot find `libpython`, install the Python development package for the interpreter you are using, then rerun the test. Local Python installs that ship only a versioned shared library may also need `LIBRARY_PATH` pointed at the directory containing `libpythonX.Y.so`.

## Check What Is Available

Run this command to see which native helpers your current wheel can use:

```bash
python -m smolvm_core
```

It prints a JSON report with four capability flags:

- `networking`: Linux TAP devices, routes, and sysctls can use direct Rust calls.
- `disk_io`: disk image copy and decompression helpers are available.
- `qmp`: QEMU monitor control is available.
- `firecracker_api`: Firecracker API socket control is available.

## Public Python Modules

Import the module for the job you want to do:

```python
from smolvm_core import capabilities, disk, firecracker, network, qmp
```

Use `capabilities.detect()` when you need one structured result:

```python
from smolvm_core import capabilities

print(capabilities.detect().as_dict())
```

Use `network` for Linux networking setup:

```python
from smolvm_core import network

network.prepare_tap("tap0", owner_uid=1000, host_ip="172.16.0.1", prefix_len=32)
network.add_route("172.16.0.2", 32, "tap0")
```

These calls require permission to change Linux networking directly, usually root or `CAP_NET_ADMIN`. If SmolVM does not have that permission, the main `smolvm` package falls back to `ip`, `nft`, and `sysctl` subprocesses so the sandbox can still work.

Use `disk` for sparse disk images:

```python
from smolvm_core import disk

method = disk.clone_or_sparse_copy("base.ext4", "sandbox.ext4")
print(method)
```

Use `qmp` for QEMU control:

```python
from pathlib import Path
from smolvm_core import qmp

with qmp.QMPClient(Path("/tmp/qmp.sock")) as client:
    client.connect()
    print(client.query_status())
```

Use `firecracker` for Firecracker API socket requests:

```python
from pathlib import Path
from smolvm_core import firecracker

client = firecracker.FirecrackerClient(Path("/tmp/firecracker.socket"))
print(client.request("GET", "/"))
```

## Migrate From The Old Flat API

This alpha refactor removes the old top-level helper aliases. Import the module for the area you need instead:

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

The compiled PyO3 module is `smolvm_core._ffi`. Treat it as an implementation detail.

Public callers should import `smolvm_core.network`, `smolvm_core.disk`, `smolvm_core.qmp`, or `smolvm_core.firecracker`. The private module can change without a compatibility alias during the alpha period.

## Rust Library Shape

The Rust crate exposes the same core areas:

- `smolvm_core::disk`
- `smolvm_core::firecracker` on Unix
- `smolvm_core::qmp`
- `smolvm_core::network` on Linux

The Python bindings live in a separate Rust binding module so the Rust library modules stay readable and testable.

## Versioning And Release Tags

`smolvm-core` uses date-based versions in `YYYY.M.D` form, such as `2026.6.24`. This keeps the same version valid for Cargo, maturin, and Python package metadata.

Release tags use the same version with a `core-v` prefix:

```bash
git tag core-v2026.6.24
git push origin core-v2026.6.24
```

The publish workflow checks that the tag matches `smolvm-core/Cargo.toml` before it builds wheels and publishes them to PyPI.
