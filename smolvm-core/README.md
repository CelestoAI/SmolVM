# smolvm-core

`smolvm-core` is the small native helper package for SmolVM. It handles low-level system work such as fast network setup and QEMU runtime control, while the main `smolvm` package keeps the public Python API.

Most users should install `smolvm`, not `smolvm-core` directly:

```bash
pip install smolvm
```

That install pulls in the matching `smolvm-core` wheel automatically on supported platforms.

Install `smolvm-core` directly only if you are developing the native extension or testing package releases.

## When the native extension is used

SmolVM uses `smolvm-core` for two native paths when they are available:

- fast host networking on Linux — creating TAP devices, adding routes, writing sysctls
- QEMU runtime control on Linux and macOS — speaking QMP, QEMU's JSON control protocol, over a Unix socket

When native host networking is unavailable, SmolVM falls back to running `ip`, `nft`, and `sysctl` as subprocesses. When native QMP is unavailable, SmolVM falls back to its pure-Python QMP client. Both fallback paths produce the same result; the native paths are faster and keep low-level protocol handling out of the public Python API.

| Scenario | Path used | What happens |
|---|---|---|
| Linux + `smolvm-core` wheel installed | Native networking + native QMP | Direct kernel calls for networking and native QMP for QEMU control. |
| Linux + wheel missing or broken | Subprocess networking + Python QMP | Fully functional, but networking falls back to `ip`/`nft`/`sysctl` subprocesses and QMP uses the Python client. |
| macOS + `smolvm-core` wheel installed | Native QMP only | macOS uses QEMU user-mode networking (SLIRP), so host networking is not exercised; QEMU control uses native QMP. |
| macOS + wheel missing or broken | Python QMP | QEMU control uses the pure-Python QMP client. |

On Linux, if SmolVM falls back to subprocess, it logs a warning at startup:

```
WARNING smolvm.host._accel: smolvm-core native extension is unavailable;
falling back to subprocess (ip/nft/sysctl) for network operations,
which is significantly slower. Reinstall smolvm to pick up the native wheel.
```

The fix is to reinstall `smolvm` so pip picks up the matching wheel for your platform.
