# SmolVM QEMU/libkrun Kernel

This directory holds the recipe for the Linux kernel SmolVM ships to
QEMU and libkrun users. QEMU runs on Linux and macOS; this kernel makes
those sandboxes boot all the way through and print log output.

## Why this exists

Our default-published kernel is fetched from
[Firecracker's CI S3 bucket][fc-ci]. It's tuned for Firecracker, which
exposes virtual hardware to the guest over a "MMIO" bus and uses an
8250 serial chip on aarch64. QEMU's `virt` machine and libkrun expose
the same devices over **PCI** instead, and use the ARM PL011 UART on
aarch64 — different drivers entirely. Empirically the Firecracker
kernel produces **zero serial output** under QEMU: it boots into a
hardware model it has no drivers for. Linux and macOS users running
QEMU or libkrun need a kernel built for that hardware.

[fc-ci]: https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.6/

## What's pinned

| File | Role |
|---|---|
| `linux.version` | Single line: the upstream Linux release we build (e.g. `6.12.10`). LTS-line for stability. |
| `linux.sha256` | One `sha256sum -c` line for the tarball at `cdn.kernel.org`. |
| `config.fragment` | Common deltas vs `x86_64_defconfig` (x86) / `defconfig` (arm64) — symbols that exist on both archs. Every line carries an inline `# why:` comment — that's the source of truth for "why is this in our kernel." |
| `config.amd64.fragment` | x86-only deltas (8250 console). Merged on top of `config.fragment` for amd64 builds. |
| `config.arm64.fragment` | arm64-only deltas (PCI host-generic, PL011 console). Merged on top of `config.fragment` for arm64 builds. |
| `build.sh` | The exact recipe CI runs. Also runnable locally — see below. |

## Building locally

```sh
cd kernel/qemu
bash build.sh
# Produces vmlinux-<host_arch>-qemu.bin in the current directory.
```

The build needs GNU Make 4.0 or newer. macOS ships an older `make`,
so install the Homebrew version with `brew install make` and run:

```sh
MAKE=gmake bash build.sh
```

Cross-builds work too if you have the toolchain:

```sh
SMOLVM_ARCH_OVERRIDE=arm64 ARCH=arm64 \
    CROSS_COMPILE=aarch64-linux-gnu- \
    bash build.sh
```

### Validating in Docker

`make defconfig` itself needs GNU `ld`, which macOS doesn't ship. Easiest
path on a Mac is to run the build in an Ubuntu container — same toolchain
CI uses. On Apple Silicon, **always pass `--platform`** (Docker silently
selects amd64 otherwise, then emulates, and the kernel build dies on
mismatched gcc flags):

```sh
# Quick: stop after fragment verification (~30 s, no kernel compile).
docker run --rm --platform=linux/arm64 \
    -v "$PWD":/src:ro -e SMOLVM_VERIFY_ONLY=1 \
    -e SMOLVM_ARCH_OVERRIDE=arm64 ubuntu:24.04 \
    bash -c 'apt-get update -qq && \
        apt-get install -y --no-install-recommends \
        build-essential bc bison flex libssl-dev libelf-dev \
        xz-utils curl ca-certificates kmod cpio python3 >/dev/null && \
        cp -r /src/kernel /tmp/kernel && \
        cd /tmp/kernel/qemu && bash build.sh'

# Full: produces a real vmlinux in /tmp/out (~5–8 min on M-series).
mkdir -p /tmp/out && docker run --rm --platform=linux/arm64 \
    -v "$PWD":/src:ro -v /tmp/out:/out -e OUT_DIR=/out \
    -e SMOLVM_ARCH_OVERRIDE=arm64 ubuntu:24.04 \
    bash -c '<same setup as above, drop SMOLVM_VERIFY_ONLY>'
```

Swap `--platform=linux/amd64` + `SMOLVM_ARCH_OVERRIDE=amd64` for the x86 build.

## Smoke-testing locally

The example below is for **macOS Apple Silicon**, which uses the
Hypervisor.framework accelerator (`hvf`). On **Linux** with KVM,
swap `accel=hvf` for `accel=kvm` and drop `-cpu host` (or keep it —
KVM accepts it too). On Linux without KVM, use `accel=tcg` and expect
slow boot.

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

# 2. One-time setup: import the kernel.org release signing keys so we can
#    verify checksums cryptographically, not just over HTTPS. Trusting only
#    HTTPS means a CDN/TLS compromise could feed us a bogus checksum file.
#    Keys and fingerprints: https://www.kernel.org/signature.html
gpg --locate-keys torvalds@kernel.org gregkh@kernel.org

# 3. Fetch the clearsigned checksum file, verify the signature, then extract
#    the line for our pinned version. `gpg --verify` exits non-zero if the
#    signature is bad or the signer isn't in your keyring.
curl -sLO https://cdn.kernel.org/pub/linux/kernel/v6.x/sha256sums.asc
gpg --verify sha256sums.asc
grep "linux-$(cat linux.version).tar.xz" sha256sums.asc > linux.sha256
rm sha256sums.asc
cat linux.sha256  # sanity check

# 4. Build locally to confirm the fragment still applies cleanly
bash build.sh
# If "Fragment verification failed", a symbol was renamed/moved in upstream
# Linux. Check the message, find the new symbol name, update fragment.

# 5. Commit and push — CI rebuilds and re-uploads the kernel.
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
  to `config.fragment` (or the per-arch fragment) as `=y`.
- **Maintenance burden.** Bumping Linux means re-running `build.sh` once
  to verify the fragment still applies, then committing. CI cache by input
  hash means the rebuild is free until inputs change.
- **Vendor independence.** We don't depend on third-party kernel publishers
  (iximiuz, etc.); we build from upstream sources only.
