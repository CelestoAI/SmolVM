# macOS desktop sandboxes

A macOS desktop sandbox gives you a temporary Mac window for opening apps, testing installers, and working with files without changing your everyday system. This preview runs locally on an Apple Silicon Mac.

## Prepare this Mac

Install the tested desktop runtime and check that it is ready:

```bash
smolvm setup --macos
smolvm doctor --backend vz
```

You need an Apple Silicon Mac running macOS 14 or newer. The image folder must be on an APFS volume, the normal file system used by modern Macs.

## Create and open a desktop

The first sandbox prepares macOS from an Apple restore file. SmolVM asks before downloading it because preparation needs about 50 GB of free space and can take 20–40 minutes. A progress bar follows the download, macOS installation, and desktop setup phases. If installation fails after the download finishes, the next attempt reuses that restore file instead of downloading it again.

```bash
smolvm sandbox create --os macos --name test-mac
# Next: smolvm sandbox desktop test-mac
```

Open the desktop:

```bash
smolvm sandbox desktop test-mac
```

SmolVM opens the built-in Screen Sharing app. The connection stays on this Mac and uses a temporary password that is not printed.

## Share files

Add one local folder when you create the sandbox:

```bash
smolvm sandbox create --os macos --name shared-mac --mount "$PWD"
```

The folder is read-only unless you add `--writable-mounts`. Use Finder inside the sandbox to open the shared folder.

## Install and use apps

Inside the desktop, macOS works normally. You can open a downloaded `.dmg`, drag an app into Applications, respond to permission prompts, and restart the sandbox when an installer requires it. SmolVM does not turn off System Integrity Protection, Gatekeeper, or macOS permission prompts.

Stop the sandbox when you are done for now:

```bash
smolvm sandbox stop test-mac
```

Delete it to discard its private disk:

```bash
smolvm sandbox delete test-mac
```

The reusable base image remains, so creating the next sandbox is much faster. Remove that image separately with `smolvm image rm macos-latest`.

## Prepare an image ahead of time

You can perform the long preparation step before creating a sandbox:

```bash
smolvm image build --os macos --ipsw latest -t macos-latest
```

The restore file comes from Apple and the finished image stays on this Mac. SmolVM does not support saving, loading, or publishing macOS images.

## Preview limits

- Only Apple Silicon hosts are supported.
- Each preview sandbox uses 4 CPU cores, 8 GB of memory, and an 80 GB logical disk; custom resource sizes are not enabled yet.
- At most two macOS sandboxes can run at once.
- Shell, exec, file transfer, and SSH tunnels are not enabled for macOS guests in this preview; desktop access works independently.
- Bridge networking, outbound-domain rules, pause and resume, snapshots, and browser sessions are not supported for macOS guests yet.
- The base image and each sandbox are tied to the local Mac. Review [Apple's current software license agreements](https://www.apple.com/legal/sla/) and use hardware you own or control for permitted development and testing.

**Implementation notes:** macOS image ownership is in [`src/smolvm/macos/images.py`](../../src/smolvm/macos/images.py), runtime translation is in [`src/smolvm/macos/lume.py`](../../src/smolvm/macos/lume.py), and lifecycle integration is covered by [`tests/test_vm_macos.py`](../../tests/test_vm_macos.py) and [`tests/test_runtime_vz.py`](../../tests/test_runtime_vz.py).
