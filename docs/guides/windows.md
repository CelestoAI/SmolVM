# Windows guests

SmolVM can run a Windows guest from a Windows disk image that you build yourself. This is useful for Windows-only software; it is a specialized QEMU workflow rather than the default sandbox path.

## Build an image

Get a Windows ISO and a virtio driver ISO, then build a reusable qcow2 image. Choose a strong password instead of the development default.

```bash
smolvm windows build-image \
  --iso ./Win11.iso \
  --virtio-win-iso ./virtio-win.iso \
  --output ./win11.qcow2 \
  --password 'choose-a-strong-password'
```

The build is unattended and can take a while. The resulting `win11.qcow2` is the input for a sandbox.

## Start a Windows sandbox

Use Python so you can provide the username and password chosen while building the image:

```python
from smolvm import SmolVM

with SmolVM(
    os="windows",
    image="./win11.qcow2",
    ssh_user="smolvm",
    ssh_password="choose-a-strong-password",
) as vm:
    result = vm.run("Get-ComputerInfo | Select-Object WindowsProductName")
    print(result.stdout)
```

The generic `smolvm sandbox create` command does not currently accept Windows login credentials, so it cannot complete the readiness check for this password-based image ([CLI options](../../src/smolvm/cli/commands/app.py), [image configuration](../../src/smolvm/facade.py)).

Windows guests are currently supported on Linux x86_64 hosts and use SSH for host-to-guest control. They do not support workspace mounts, outbound-domain controls, or snapshots.

## Implementation notes

The image builder creates the unattended install media and waits for the installed guest in [`src/smolvm/windows/build_image.py`](../../src/smolvm/windows/build_image.py). Windows platform settings are in [`src/smolvm/runtime/guest_platforms.py`](../../src/smolvm/runtime/guest_platforms.py); configuration restrictions are enforced in [`src/smolvm/facade.py`](../../src/smolvm/facade.py) and [`src/smolvm/vm.py`](../../src/smolvm/vm.py). See [`tests/test_windows_build_image.py`](../../tests/test_windows_build_image.py).
