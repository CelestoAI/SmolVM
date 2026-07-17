# Networking

SmolVM can make a service inside a sandbox available on your machine and, on supported sandboxes, limit which internet domains it can reach.

## Share a sandbox service

If a service listens on port 3000 in sandbox `demo`, share it locally:

```bash
smolvm sandbox port expose demo 3000
```

Use the returned host port in your browser or tool. `smolvm sandbox port list demo` shows active mappings. In `smolvm sandbox port close demo HOST_PORT:3000`, replace `HOST_PORT` with the returned host port—for example, `smolvm sandbox port close demo 49152:3000`.

## Connect a sandbox directly to an existing network

On Linux, a sandbox can appear as a separate computer on a network you already configured. This advanced mode gives the sandbox its own network identity instead of placing it behind SmolVM's private network.

The host must already have a Linux bridge, a host network interface that joins several connections into one network. It must be connected to the target network, and neither the bridge nor its member interfaces may have host addresses, including automatic IPv6 addresses. SmolVM checks this setup but never creates, reconfigures, or deletes the bridge.

Check bridge `br10` before creating a sandbox:

```bash
smolvm bridge check br10
# Bridge 'br10' is ready for bridged networking.
```

Create a bridged sandbox only after that check passes:

```bash
smolvm sandbox create --name demo --os alpine --network bridge --bridge br10
```

The current SmolVM Alpine image automatically asks the network for an address using DHCP (Dynamic Host Configuration Protocol). To use a static address instead, add an executable `/etc/smolvm/network.sh` script inside the guest disk. SmolVM passes `eth0` as the script's first argument each time the guest boots. You can open the guest before it has an address because `smolvm sandbox shell demo` uses a direct host-to-guest control channel rather than the network.

Custom images must understand the `smolvm.network=guest` boot setting and configure `eth0`. When creating `VMConfig` directly for a compatible image, set `guest_managed_networking=True`. SmolVM rejects older published or custom images instead of starting them without working bridge configuration.

Bridge mode deliberately does not provide SmolVM NAT, port exposure, SSH from the host, workspace mounts, or outbound-domain controls. Connect to guest services from the bridged network, and use `smolvm sandbox shell demo` for host administration.

A bridged sandbox can send traffic directly to the selected network. Configuration mistakes or untrusted guest software can affect other devices through duplicate addresses, address spoofing, or unwanted services. Use this mode only on a network where that access is acceptable.

## Limit outbound domains in Python

Pass an allow-list when creating a sandbox:

```python
from smolvm import SmolVM

with SmolVM(internet_settings={"allowed_domains": ["api.example.com"]}) as vm:
    vm.run("curl https://api.example.com")
```

Use `"*"` to allow all domains, which is the default. Entries may be hostnames or URLs without a path; SmolVM stores their hostnames. HTTP method restrictions are reserved for future work and are not enforced.

## Current limits

Outbound-domain controls require SmolVM's private TAP networking: Firecracker uses it by default, and custom QEMU configurations can opt in with `VMConfig.qemu_network="tap"`. They do not apply to bridged networking, QEMU user-mode networking, libkrun, or Windows guests. Treat an allow-list as a network control, not as a complete security boundary for untrusted code.

**Implementation notes:** the public settings and validation are in [`src/smolvm/types.py`](../../src/smolvm/types.py), backend networking selection is in [`src/smolvm/vm.py`](../../src/smolvm/vm.py), and host rules are applied in [`src/smolvm/host/network.py`](../../src/smolvm/host/network.py). Behavior is covered by [`tests/test_internet_settings.py`](../../tests/test_internet_settings.py) and [`tests/test_network.py`](../../tests/test_network.py).
