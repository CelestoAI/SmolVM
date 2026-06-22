# smolvm-core

Most projects should use `smolvm`. It installs `smolvm-core` automatically so sandboxes can start and be controlled efficiently on supported hosts. Import `smolvm-core` directly only when you are working on that package itself.

Most users should install `smolvm`, not `smolvm-core` directly:

```bash
pip install smolvm
```

That install pulls in the matching `smolvm-core` wheel automatically on supported platforms.

Install `smolvm-core` directly only if you are developing the native extension or testing package releases.

## Versioning and release tags

`smolvm-core` uses date-based versions in `YYYY.M.D` form, such as `2026.6.22`. This keeps the same version valid for Cargo, maturin, and Python package metadata.

Release tags use the same version with a `core-v` prefix:

```bash
git tag core-v2026.6.22
git push origin core-v2026.6.22
```

The publish workflow checks that the tag matches `smolvm-core/Cargo.toml` before it builds wheels and publishes them to PyPI.

## When the native extension is used

SmolVM uses `smolvm-core` for two host-side jobs when they are available:

- Linux sandbox networking setup — creating TAP devices (virtual network cards), adding routes, and writing sysctls (kernel settings)
- QEMU sandbox control on Linux and macOS — speaking QMP, QEMU's JSON control protocol, over a Unix socket

When native host networking is unavailable, or when the SmolVM process lacks permission to change Linux networking directly, SmolVM falls back to running `ip`, `nft`, and `sysctl` as subprocesses. When native QMP is unavailable, SmolVM falls back to its pure-Python QMP client. Both fallback paths produce the same result; the native paths are faster and keep low-level protocol handling out of the main Python API.

| Scenario | Path used | What happens |
|---|---|---|
| Linux + `smolvm-core` wheel installed + root/CAP_NET_ADMIN | Native networking + native QMP | Direct kernel calls for networking and native QMP for QEMU control. |
| Linux + wheel installed, but no direct networking permission | Subprocess networking + native QMP | SmolVM falls back to `ip`/`nft`/`sysctl` subprocesses for networking; native QMP still works. |
| Linux + wheel missing or broken | Subprocess networking + Python QMP | Fully functional, but networking falls back to `ip`/`nft`/`sysctl` subprocesses and QMP uses the Python client. |
| macOS + `smolvm-core` wheel installed | Native QMP only | macOS uses QEMU user-mode networking (SLIRP), so host networking is not exercised; QEMU control uses native QMP. |
| macOS + wheel missing or broken | Python QMP | QEMU control uses the pure-Python QMP client. |

On Linux, missing native support and missing networking permission produce different warnings. If the native wheel is missing or broken, SmolVM logs:

```
WARNING smolvm.host._accel: smolvm-core native extension is unavailable;
falling back to subprocess (ip/nft/sysctl) for network operations,
which is significantly slower. Reinstall smolvm to pick up the native wheel.
```

The fix is to reinstall `smolvm` so pip picks up the matching wheel for your platform.

If the wheel is installed but the process cannot change Linux networking directly, SmolVM logs a permission warning and uses the slower sudo fallback. Run `smolvm setup` if the fallback is missing, or start the same SmolVM command as root or with CAP_NET_ADMIN to use the native networking speedup.

## Public Python interface

The supported direct Python interface is intentionally small:

```python
import smolvm_core

smolvm_core.has_native_networking()  # Linux native network helpers
smolvm_core.has_native_qmp()         # private native QMP accelerator is present
smolvm_core.is_available()           # compatibility alias for has_native_networking()
```

Linux networking helpers:

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

`configure_tap()` is the preferred helper when setting up a TAP for a sandbox. It flushes addresses, assigns the host IP, and brings the link up in one native call. SmolVM still writes per-TAP sysctls from Python so fallback behavior stays the same.

These helpers raise `OSError("Not available on this platform")` when the operation does not exist on the current operating system. SmolVM catches that and uses the portable fallback path where one exists.

`smolvm_core.is_available()` used to mean "the native extension is useful." That was ambiguous on macOS because native QMP can be available while native networking is not. New code should call `has_native_networking()` or `has_native_qmp()` instead.

## QMP boundary

QMP support inside `smolvm-core` is private. Use `smolvm.qmp.QMPClient` from the main package for QEMU monitor operations:

```python
from pathlib import Path
from smolvm.qmp import QMPClient

with QMPClient(Path("/tmp/qmp.sock")) as qmp:
    qmp.connect()
    print(qmp.query_status())
```

`QMPClient` chooses the native accelerator when it is installed and falls back to the pure-Python implementation when it is not. It also normalizes native exceptions into `SmolVMError`, so callers see one stable error contract.
