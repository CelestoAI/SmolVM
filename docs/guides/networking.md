# Networking

SmolVM can make a service inside a sandbox available on your machine and, on supported sandboxes, limit which internet domains it can reach.

## Share a sandbox service

If a service listens on port 3000 in sandbox `demo`, share it locally:

```bash
smolvm sandbox port expose demo 3000
```

Use the returned host port in your browser or tool. `smolvm sandbox port list demo` shows active mappings. In `smolvm sandbox port close demo HOST_PORT:3000`, replace `HOST_PORT` with the returned host port—for example, `smolvm sandbox port close demo 49152:3000`.

## Limit outbound domains in Python

Pass an allow-list when creating a sandbox:

```python
from smolvm import SmolVM

with SmolVM(internet_settings={"allowed_domains": ["api.example.com"]}) as vm:
    vm.run("curl https://api.example.com")
```

Use `"*"` to allow all domains, which is the default. Entries may be hostnames or URLs without a path; SmolVM stores their hostnames. HTTP method restrictions are reserved for future work and are not enforced.

## Current limits

Outbound-domain controls require host TAP networking: Firecracker uses it by default, and custom QEMU configurations can opt in with `VMConfig.qemu_network="tap"`. They do not apply to QEMU user-mode networking, libkrun, or Windows guests. Treat an allow-list as a network control, not as a complete security boundary for untrusted code.

**Implementation notes:** the public settings and validation are in [`src/smolvm/types.py`](../../src/smolvm/types.py), backend networking selection is in [`src/smolvm/vm.py`](../../src/smolvm/vm.py), and host rules are applied in [`src/smolvm/host/network.py`](../../src/smolvm/host/network.py). Behavior is covered by [`tests/test_internet_settings.py`](../../tests/test_internet_settings.py) and [`tests/test_network.py`](../../tests/test_network.py).
