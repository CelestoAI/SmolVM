# SmolVM QEMU/libkrun Kernel

This directory pins the inputs to a custom Linux kernel SmolVM builds in CI
for the published-image pipeline. It's the **QEMU/libkrun** variant — built
for hypervisors that expose virtio over **PCI** (vs Firecracker's MMIO bus)
and use the ARM PL011 UART on aarch64 (vs Firecracker's 8250-MMIO).

## Why this exists

Our default-published kernel is fetched from
[Firecracker's CI S3 bucket][fc-ci]. It's tuned for Firecracker — virtio-MMIO
+ 8250 UART, no PCI, no PL011. Empirically that kernel produces **zero serial
output** under QEMU `virt` machine: the kernel boots into a hardware model it
has no drivers for. macOS users have to use QEMU (or libkrun) — they need a
kernel built for that hardware, not Firecracker's.

[fc-ci]: https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.6/

## What's pinned

| File | Role |
|---|---|
| `linux.version` | Single line: the upstream Linux release we build (e.g. `6.12.10`). LTS-line for stability. |
| `linux.sha256` | One `sha256sum -c` line for the tarball at `cdn.kernel.org`. |
| `config.fragment` | Our deltas vs `microvm_defconfig` (x86) / `defconfig` (arm64). Every line carries an inline `# why:` comment — that's the source of truth for "why is this in our kernel." |
| `build.sh` | The exact recipe CI runs. Also runnable locally — see below. |

## Building locally

```sh
cd kernel/qemu
bash build.sh
# Produces vmlinux-<host_arch>-qemu.bin in the current directory.
```

Cross-builds work too if you have the toolchain:

```sh
SMOLVM_ARCH_OVERRIDE=arm64 ARCH=arm64 \
    CROSS_COMPILE=aarch64-linux-gnu- \
    bash build.sh
```

## Smoke-testing locally

```sh
qemu-system-aarch64 -machine virt,accel=hvf -cpu host -smp 2 -m 1024 \
    -kernel vmlinux-arm64-qemu.bin \
    -drive file=/path/to/openclaw/rootfs.ext4,format=raw,if=none,id=root \
    -device virtio-blk-pci,drive=root \
    -netdev user,id=net0 -device virtio-net-pci,netdev=net0 \
    -append "console=ttyAMA0 reboot=k panic=1 init=/init root=/dev/vda rw" \
    -nographic -no-reboot
```

Expected: kernel boot messages, `/init` log lines, sshd listening on
`10.0.2.15:22`. If you see `<<< pl011 console >>>` text but the boot stalls,
check the rootfs has a valid `/init`. If you see nothing at all, check
`config.fragment` against the actual `.config` (also written to
`vmlinux-<arch>-qemu.config` next to the artifact).

## Updating Linux

```sh
# 1. Pick a newer 6.12.x patch from https://kernel.org
echo 6.12.X > linux.version

# 2. Fetch the official sha256sum for your version
curl -sL https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc \
    | grep "linux-$(cat linux.version).tar.xz" \
    > linux.sha256
cat linux.sha256  # sanity check

# 3. Build locally to confirm the fragment still applies cleanly
bash build.sh
# If "Fragment verification failed", a symbol was renamed/moved in upstream
# Linux. Check the message, find the new symbol name, update fragment.

# 4. Commit and push — CI rebuilds and re-uploads the kernel.
```

## Naming convention (asymmetric, by design)

The artifact is named `vmlinux-<arch>-qemu.bin` — preset-independent. The
existing **Firecracker** artifacts are named `<preset>-<arch>-vmlinux.bin`
— per-preset, even though the kernel itself doesn't depend on the preset.
That asymmetry is intentional for now: the kernel really is preset-agnostic
and we don't want to encode that fiction into the new naming. Cleanup of the
older Firecracker naming is a future task.

## Constraints and tradeoffs

- **No modules built.** `# CONFIG_MODULES is not set` ensures every driver
  needed at boot is `=y` (in-kernel). Without modules we don't need an
  initrd, which keeps the image set simple. Cost: any future preset that
  needs a kernel module (zfs, btrfs, NFS, etc.) requires adding the symbol
  to `config.fragment` as `=y`.
- **Maintenance burden.** Bumping Linux means re-running `build.sh` once
  to verify the fragment still applies, then committing. CI cache by input
  hash means the rebuild is free until inputs change.
- **Vendor independence.** We don't depend on third-party kernel publishers
  (iximiuz, etc.); we build from upstream sources only.
