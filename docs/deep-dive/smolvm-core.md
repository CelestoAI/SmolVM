# smolvm-core

`smolvm-core` helps SmolVM prepare sandbox networking and control local virtual machines efficiently on supported hosts. Most application developers use it through `smolvm`; direct imports are only for people working on this helper package or checking which native features are available.

## What It Does

`smolvm-core` is a Rust extension loaded by the Python package. It handles two jobs:

- Linux networking helpers for TAP devices, routes, and sysctls.
- A private QMP accelerator for QEMU monitor commands.

QMP means QEMU Machine Protocol. It is the JSON protocol SmolVM uses to pause, resume, snapshot, and inspect QEMU sandboxes.

## What To Import

Application code should use the main package:

```python
from smolvm import SmolVM
```

Native-extension contributors can import `smolvm_core` directly for capability checks:

```python
import smolvm_core

print(smolvm_core.has_native_networking())
print(smolvm_core.has_native_qmp())
```

Use `has_native_networking()` when you need to know whether the Linux networking helpers are active. Use `has_native_qmp()` when you need to know whether the private native QMP accelerator was built into the wheel.

`is_available()` is still available for older callers, but it only means native Linux networking is available.

## Public Functions

These direct functions are supported for native networking work:

```python
smolvm_core.create_tap(name: str, owner_uid: int) -> None
smolvm_core.delete_tap(name: str) -> None
smolvm_core.flush_addrs(name: str) -> None
smolvm_core.add_addr(name: str, ip: str, prefix_len: int) -> None
smolvm_core.set_link_up(name: str) -> None
smolvm_core.configure_tap(name: str, host_ip: str, prefix_len: int) -> None
smolvm_core.add_route(dest: str, prefix_len: int, dev: str) -> None
smolvm_core.get_default_interface() -> str
smolvm_core.write_sysctl(key: str, value: str) -> None
```

Use `configure_tap()` for normal TAP setup. It combines address flush, address assignment, and link-up into one native operation. SmolVM still writes the per-TAP sysctl from Python after this call.

On macOS, the networking helpers raise `OSError("Not available on this platform")`. That is expected. SmolVM uses QEMU user-mode networking on macOS, so it does not need TAP setup there.

## QMP Is Private

Do not import `_QmpClient` from `smolvm_core._smolvm_core`. It is an implementation detail.

Use the stable Python wrapper instead:

```python
from pathlib import Path
from smolvm.qmp import QMPClient

with QMPClient(Path("/tmp/qmp.sock")) as qmp:
    qmp.connect()
    print(qmp.query_status())
```

`QMPClient` uses the Rust implementation when it is installed and falls back to the pure-Python implementation when it is not. It also converts native failures into `SmolVMError`, so callers do not need separate error handling for the two paths.
