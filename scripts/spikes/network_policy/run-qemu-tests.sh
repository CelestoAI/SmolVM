#!/usr/bin/env bash
# Disposable ARM64 QEMU/TCG integration environment. Never use host networking,
# --privileged or host mounts. No customer data or credentials are passed in.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p _assets
release=https://github.com/CelestoAI/SmolVM/releases/download/images-2026.09.07.0
if [[ ! -f _assets/vmlinux.bin ]]; then
  curl --fail --location --proto '=https' "$release/vmlinux-arm64.image" -o _assets/vmlinux.bin.part
  mv _assets/vmlinux.bin.part _assets/vmlinux.bin
fi
if [[ ! -f _assets/rootfs.ext4.zst ]]; then
  curl --fail --location --proto '=https' "$release/ubuntu-arm64-rootfs.ext4.zst" -o _assets/rootfs.ext4.zst.part
  mv _assets/rootfs.ext4.zst.part _assets/rootfs.ext4.zst
fi
# Dockerfile.qemu verifies both SHA-256 pins before decompressing or booting.
docker build --build-context policy=../../../src/smolvm/network_policy -f Dockerfile.qemu -t smolvm-network-policy-qemu .
exec docker run --rm --network none --cap-drop ALL \
  --cap-add NET_ADMIN --cap-add SETUID --cap-add SETGID --cap-add SETPCAP \
  --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add KILL --device /dev/net/tun \
  --security-opt no-new-privileges --sysctl net.ipv4.ip_forward=1 \
  --sysctl net.ipv4.conf.all.rp_filter=0 --sysctl net.ipv4.conf.default.rp_filter=0 \
  --add-host allowed.example:11.0.0.2 --add-host denied.example:11.0.0.2 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=512m --memory 2g --cpus 4 \
  -e SMOLVM_POLICY_QEMU_TESTS=1 smolvm-network-policy-qemu
