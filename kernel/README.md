# SmolVM Guest Kernel

Builds a purpose-built Linux kernel for the SmolVM guest. Published as a versioned GitHub Release asset (`smolvm-kernel-v${VERSION}-${arch}.tar.zst`) and consumed by the facade at boot time.

## Why we build our own

Upstream distro kernels ship 9p, virtio-fs, and overlayfs as *modules*. When the kernel version on disk (`/lib/modules/$(uname -r)/`) doesn't match the booted kernel — a routine occurrence when pairing a kernel from one upstream tree with a rootfs from another — `modprobe` silently fails and features that look present on paper don't load. SmolVM hit exactly this with `--mount`.

Own kernel, own `.config`, `MODULES=n`. Zero module-skew surface. One `(kernel, rootfs)` tuple per release, tested together.

## Design

- **Base:** mainline Linux **LTS** (pinned in [`LINUX_VERSION`](LINUX_VERSION), sha256 in [`LINUX_SHA256`](LINUX_SHA256)).
- **Config:** full `.config` per arch under [`config/`](config/) — not a fragment merged over `defconfig`, because fragments drift silently when `olddefconfig` runs.
- **Serves:** QEMU (x86_64 + aarch64) and Firecracker (x86_64 + aarch64). libkrun ships its own kernel and isn't affected by this build.
- **Format per arch:**
  - x86_64: `vmlinux` (uncompressed ELF — Firecracker requires ELF; QEMU boots it too)
  - aarch64: `Image` (uncompressed PE — both backends consume it)

## Versioning

Two independent version strings:

| File | Example | Meaning |
|---|---|---|
| [`VERSION`](VERSION) | `1.0.0` | SmolVM kernel release tag. Bumped when the config or toolchain changes. |
| [`LINUX_VERSION`](LINUX_VERSION) | `6.12.82` | Upstream Linux point release. Bumped on LTS security rolls. |

Bumping `LINUX_VERSION` (e.g., for a CVE fix) usually means bumping `VERSION` to `1.0.1` as well, because the resulting kernel binary is different.

## Releasing

1. Update [`LINUX_VERSION`](LINUX_VERSION) and [`LINUX_SHA256`](LINUX_SHA256) (from `cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc`).
2. Bump [`VERSION`](VERSION).
3. Commit and tag: `git tag kernel-v$(cat kernel/VERSION) && git push --tags`.
4. The [`kernel-release.yml`](../.github/workflows/kernel-release.yml) workflow builds both arches and attaches the tarballs to the GitHub Release.

## Building locally

```
cd kernel
docker build -t smolvm-kernel-builder .
docker run --rm -v "$PWD:/work" smolvm-kernel-builder /work/scripts/build.sh aarch64
```

Outputs land in `kernel/build/smolvm-kernel-v${VERSION}-${arch}.tar.zst`.

## What's in the config

See the **Virtio capability audit** in the migration plan for the canonical `=y` / `=n` matrix. Highlights:

- `=y`: `VIRTIO_PCI`, `VIRTIO_MMIO`, `VIRTIO_BLK`, `VIRTIO_NET`, `VIRTIO_CONSOLE`, `VIRTIO_FS`, `VIRTIO_VSOCK`, `VIRTIO_RNG`, `FUSE_FS`, `OVERLAY_FS`, `EXT4_FS`
- `=n`: `VIRTIO_BALLOON`, `VIRTIO_GPU`, `VIRTIO_INPUT`, `VIRTIO_SCSI`, `VIRTIO_CRYPTO`, `VIRTIO_PMEM`, `NET_9P`, `9P_FS`, `MODULES`

`MODULES=n` is the invariant that kills kernel/rootfs version skew for good.
