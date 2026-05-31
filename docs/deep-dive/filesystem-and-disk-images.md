# Filesystem & Disk Images

## The Big Picture

A microVM needs two things to boot: a **kernel** (`.bin` file) and a **root filesystem** (where the OS files live). The root filesystem is a single file on the host that acts as the VM's entire hard drive — like a ZIP of an entire Linux installation. The VM sees it as a real disk.

---

## Filesystem Type: ext4 (both backends)

Both Firecracker and QEMU use **ext4** as the root filesystem format — the same filesystem most Linux servers use on real hardware.

The rootfs lives in a file called `rootfs.ext4`:

```
~/.smolvm/images/{image-name}/
├── vmlinux.bin      ← the kernel
└── rootfs.ext4      ← the "hard drive" (a raw ext4 image)
```

A raw image file is exactly "a big file that is a virtual disk." The kernel sees it as a block device, just like an SSD. `mkfs.ext4` is run on it to format it, the same as formatting a real disk. Default size is 512 MB (configurable per image builder).

**Image building** (`build.py`):
- Linux: `mkfs.ext4` via loop device (`_create_ext4_with_loopfs`)
- macOS: Docker + `mke2fs` since macOS lacks native loop device support (`_create_ext4_with_docker`)

---

## Per-VM Disk: How the Backends Differ

The shared `rootfs.ext4` base image is never modified directly. Each VM gets its own disk copy so VMs don't interfere with each other. This is where the backends diverge:

| | Firecracker | QEMU |
|---|---|---|
| **Per-VM disk format** | `.ext4` (raw copy) | `.qcow2` (QEMU Copy-on-Write) |
| **How it's created** | Direct file copy | `qemu-img convert` from ext4 → qcow2 |
| **Stored at** | `~/.smolvm/data/disks/{id}.ext4` | `~/.smolvm/data/disks/{id}.qcow2` |
| **Drive attachment** | Firecracker API `add_drive()` | QEMU `-drive format=qcow2` |

**qcow2** is QEMU's native format. It supports thin provisioning (only allocates space for data actually written), which is why QEMU converts to it. Firecracker keeps it simple with raw ext4.

---

## Disk Modes

Controlled via `VMConfig.disk_mode`:

- **`isolated`** (default): Each VM gets its own disk copy — changes are sandboxed, no VM affects another. Can be retained after VM deletion with `retain_disk_on_delete=True`.
- **`shared`**: All VMs boot from the same base `rootfs.ext4` directly — faster startup, but no per-VM isolation.

---

## Extra Drives

Additional block devices can be attached to either backend via `VMConfig.extra_drives`. Format is auto-detected by file extension:

| Extension | Format | Notes |
|---|---|---|
| `.qcow2` | qcow2 | |
| `.iso` | raw | read-only |
| other | raw | |

Firecracker attaches them as `data_drive`, `data_drive_1`, etc. QEMU uses additional `-drive` parameters.

---

## Snapshots

| | Firecracker | QEMU |
|---|---|---|
| **State file** | `vmstate.bin` | internal to QEMU |
| **Memory file** | `mem.bin` | included in QEMU snapshot |
| **Disk file** | `disk.ext4` | `disk.qcow2` |

### Snapshot types: full vs diff

When you create a snapshot you can choose how much of the disk to store with `--snapshot-type` (CLI) or `snapshot_type=` (SDK):

- **`full`** (default): a complete, self-contained copy of the disk. It restores on its own even if the original base image is gone — the safest, most portable choice, and the right default for everyday use.
- **`diff`**: stores only what changed since the shared base image, so it takes far less space. On QEMU the snapshot keeps the thin qcow2 overlay; on Firecracker the disk is cloned with a copy-on-write reflink on filesystems that support it (btrfs, XFS, ZFS, APFS), falling back to a full copy elsewhere. The trade-off: a diff snapshot needs its base image to still be present to restore. Best for production systems that take many snapshots and keep their base images in place.

On Firecracker the VM state and memory files are always captured in full; `diff` only changes how the disk is stored.

---

## Built-in Images

| Image | Base OS | Default Rootfs Size |
|---|---|---|
| `hello` | Alpine Linux + SSH | 512 MB |
| `quickstart-x86_64` | Ubuntu Bionic + SSH | 512 MB |

Larger rootfs builders exist for browser (`4 GB`) and Node.js/OpenClaw (`2 GB`) use cases.

---

## Key Source Files

| File | What it covers |
|---|---|
| `src/smolvm/build.py` | ext4 image creation (lines 1333–1524) |
| `src/smolvm/vm.py` | Disk lifecycle, qemu-img conversion (lines 319–362, 1292–1450) |
| `src/smolvm/runtime_firecracker.py` | Firecracker drive/snapshot handling |
| `src/smolvm/runtime_qemu.py` | QEMU drive/snapshot handling |
| `src/smolvm/types.py` | `VMConfig`, `disk_mode` definitions |
| `src/smolvm/images.py` | Image caching and validation |
