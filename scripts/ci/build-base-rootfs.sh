#!/usr/bin/env bash
# Build the shared base rootfs ext4 image from a Dockerfile.
#
# Usage:  build-base-rootfs.sh <output-dir> [size-mb]
# Env:    OS=ubuntu|alpine (default: ubuntu)
#
# Produces: <output-dir>/base-rootfs.ext4
#
# Runs in CI on a matching-arch runner (no cross-compilation).
# Requires: docker, mkfs.ext4, mount (loop device support), e2fsck,
# resize2fs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${1:?Usage: build-base-rootfs.sh <output-dir> [size-mb]}"
OS="${OS:-ubuntu}"

case "$OS" in
  ubuntu)
    DOCKERFILE="$SCRIPT_DIR/Dockerfile.base-rootfs"
    DEFAULT_SIZE_MB=4096
    ;;
  alpine)
    DOCKERFILE="$SCRIPT_DIR/Dockerfile.base-alpine-rootfs"
    # Alpine + musl + busybox lands around 200 MB — a 1 GiB ext4 is plenty
    # of headroom for the preset layer to install on top. We shrink to the
    # actual contents below with resize2fs -M, so this is just an upper
    # bound for the temporary mkfs scratch space.
    DEFAULT_SIZE_MB=1024
    ;;
  *)
    echo "Unsupported OS: $OS (expected: ubuntu | alpine)" >&2
    exit 1
    ;;
esac

SIZE_MB="${2:-$DEFAULT_SIZE_MB}"

mkdir -p "$OUT_DIR"

TAG="smolvm-base-rootfs-${OS}"

echo "==> Building $OS base rootfs Docker image..."
docker build -t "$TAG" -f "$DOCKERFILE" "$SCRIPT_DIR"

echo "==> Exporting container filesystem..."
CID=$(docker create "$TAG" /bin/true)
MNT=""
cleanup() {
  # Fire-and-forget: errors past the first failing step stop the script,
  # but cleanup must still try every resource. Loop mount must come down
  # before the rmdir, and the docker container is independent of both.
  if [ -n "$MNT" ] && mountpoint -q "$MNT" 2>/dev/null; then
    umount "$MNT" 2>/dev/null || true
  fi
  if [ -n "$MNT" ] && [ -d "$MNT" ]; then
    rmdir "$MNT" 2>/dev/null || true
  fi
  if [ -n "${CID:-}" ]; then
    docker rm -f "$CID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
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
MNT=""

rm -f "$OUT_DIR/rootfs.tar"

# Shrink the ext4 to its actual contents so the published artifact carries
# only what's used. e2fsck must precede resize2fs -M; the truncate after
# resize trims the host-side file to the new filesystem size (resize2fs
# leaves the underlying file size untouched).
echo "==> Shrinking ext4 to actual usage..."
e2fsck -fy "$OUT_DIR/base-rootfs.ext4" >/dev/null 2>&1
resize2fs -M "$OUT_DIR/base-rootfs.ext4" 2>&1 | tail -1
# Read the resulting block count + block size out of dumpe2fs and truncate
# the host file to match.
BLOCK_COUNT=$(dumpe2fs -h "$OUT_DIR/base-rootfs.ext4" 2>/dev/null | awk -F: '/^Block count/{gsub(/ /,"",$2); print $2}')
BLOCK_SIZE=$(dumpe2fs -h "$OUT_DIR/base-rootfs.ext4" 2>/dev/null | awk -F: '/^Block size/{gsub(/ /,"",$2); print $2}')
if [ -n "$BLOCK_COUNT" ] && [ -n "$BLOCK_SIZE" ]; then
  FINAL_BYTES=$((BLOCK_COUNT * BLOCK_SIZE))
  truncate -s "$FINAL_BYTES" "$OUT_DIR/base-rootfs.ext4"
fi

echo "==> Base rootfs ($OS): $OUT_DIR/base-rootfs.ext4 ($(du -sh "$OUT_DIR/base-rootfs.ext4" | cut -f1))"
