# smolvm-core

`smolvm-core` is the small native helper package for SmolVM. It handles low-level system work such as fast network setup, while the main `smolvm` package keeps the public Python API.

Most users should install `smolvm`, not `smolvm-core` directly:

```bash
pip install smolvm
```

That install pulls in the matching `smolvm-core` wheel automatically on supported platforms.

Install `smolvm-core` directly only if you are developing the native extension or testing package releases.
