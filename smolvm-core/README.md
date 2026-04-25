# smolvm-core

`smolvm-core` is the small native helper package for SmolVM. It handles low-level system work such as fast network setup, while the main `smolvm` package keeps the public Python API.

Most users should install `smolvm`, not `smolvm-core` directly:

```bash
pip install smolvm
```

That install pulls in the matching `smolvm-core` wheel automatically on supported platforms.

Install `smolvm-core` directly only if you are developing the native extension or testing package releases.

## When the native extension is used

SmolVM uses `smolvm-core` for fast host networking — creating TAP devices, adding routes, writing sysctls — when it's available. When it isn't, SmolVM falls back to running `ip`, `nft`, and `sysctl` as subprocesses. Both paths produce the same result; the native path is just much faster.

| Scenario | Path used | What happens |
|---|---|---|
| Linux + `smolvm-core` wheel installed | Native (netlink) | Direct kernel calls — microseconds per operation. |
| Linux + wheel missing or broken | Subprocess | Forks `ip`/`nft`/`sysctl` for each operation. Fully functional but significantly slower; VM creation takes longer. |
| macOS | Path never reached | macOS uses the QEMU backend with user-mode networking (SLIRP), so the host-networking code that would call `smolvm-core` isn't exercised. |

On Linux, if SmolVM falls back to subprocess, it logs a warning at startup:

```
WARNING smolvm.host._accel: smolvm-core native extension is unavailable;
falling back to subprocess (ip/nft/sysctl) for network operations,
which is significantly slower. Reinstall smolvm to pick up the native wheel.
```

The fix is to reinstall `smolvm` so pip picks up the matching wheel for your platform.
