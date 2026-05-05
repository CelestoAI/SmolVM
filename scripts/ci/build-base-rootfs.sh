#!/usr/bin/env bash
# Build the shared base rootfs ext4 image from Dockerfile.base-rootfs.
#
# Usage:  build-base-rootfs.sh <output-dir> [size-mb]
#
# Produces: <output-dir>/base-rootfs.ext4
#
# Runs in CI on a matching-arch runner (no cross-compilation).
# Requires: docker, mkfs.ext4, mount (loop device support).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${1:?Usage: build-base-rootfs.sh <output-dir> [size-mb]}"
SIZE_MB="${2:-4096}"

mkdir -p "$OUT_DIR"

TAG="smolvm-base-rootfs"

echo "==> Building base rootfs Docker image..."
docker build -t "$TAG" -f "$SCRIPT_DIR/Dockerfile.base-rootfs" "$SCRIPT_DIR"

echo "==> Exporting container filesystem..."
CID=$(docker create "$TAG" /bin/true)
trap 'docker rm -f "$CID" >/dev/null 2>&1 || true' EXIT
docker export "$CID" > "$OUT_DIR/rootfs.tar"

echo "==> Creating ${SIZE_MB}M ext4 image..."
dd if=/dev/zero of="$OUT_DIR/base-rootfs.ext4" bs=1M count=0 seek="$SIZE_MB" 2>/dev/null
mkfs.ext4 -F -L smolvm-rootfs "$OUT_DIR/base-rootfs.ext4" >/dev/null 2>&1

MNT=$(mktemp -d)
mount -o loop "$OUT_DIR/base-rootfs.ext4" "$MNT"
tar -xf "$OUT_DIR/rootfs.tar" -C "$MNT" \
  --exclude='dev/*' --exclude='proc/*' --exclude='sys/*' --exclude='.dockerenv'
umount "$MNT"
rmdir "$MNT"

rm -f "$OUT_DIR/rootfs.tar"

echo "==> Base rootfs: $OUT_DIR/base-rootfs.ext4 ($(du -sh "$OUT_DIR/base-rootfs.ext4" | cut -f1))"
