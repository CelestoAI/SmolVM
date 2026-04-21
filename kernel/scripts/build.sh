#!/usr/bin/env bash
# Build the SmolVM guest kernel for a given target architecture.
#
# Usage:
#   build.sh <x86_64|aarch64>
#
# Produces: kernel/build/smolvm-kernel-v${VERSION}-${arch}.tar.zst
# Tarball contents (see kernel/README.md for rationale):
#   - x86_64:  vmlinux (uncompressed ELF, Firecracker-compatible)
#   - aarch64: Image   (uncompressed PE,  both QEMU and Firecracker)
#   - Both:    config, SHA256SUMS, LINUX_VERSION

set -euo pipefail

# ---------------------------------------------------------------------------
# Locate the kernel/ directory regardless of where the script is invoked from.
# Inside the container this is /work; in CI checkout it's $GITHUB_WORKSPACE/kernel.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Parse args.
# ---------------------------------------------------------------------------
if [[ $# -ne 1 ]]; then
    echo "usage: $0 <x86_64|aarch64>" >&2
    exit 2
fi
TARGET_ARCH="$1"

case "${TARGET_ARCH}" in
    x86_64)
        KERNEL_ARCH="x86_64"        # what we pass to make ARCH=
        KERNEL_IMAGE_PATH="vmlinux" # relative to kernel source root
        KERNEL_IMAGE_NAME="vmlinux"
        ;;
    aarch64)
        KERNEL_ARCH="arm64"
        KERNEL_IMAGE_PATH="arch/arm64/boot/Image"
        KERNEL_IMAGE_NAME="Image"
        ;;
    *)
        echo "unsupported arch: ${TARGET_ARCH} (expected x86_64 or aarch64)" >&2
        exit 2
        ;;
esac

# ---------------------------------------------------------------------------
# Read pinned versions.
# ---------------------------------------------------------------------------
VERSION="$(< "${KERNEL_DIR}/VERSION" tr -d '[:space:]')"
LINUX_VERSION="$(< "${KERNEL_DIR}/LINUX_VERSION" tr -d '[:space:]')"
LINUX_SHA256="$(< "${KERNEL_DIR}/LINUX_SHA256" tr -d '[:space:]')"

echo "=== SmolVM kernel build ==="
echo "  SmolVM kernel version : ${VERSION}"
echo "  Linux version         : ${LINUX_VERSION}"
echo "  Target arch           : ${TARGET_ARCH} (ARCH=${KERNEL_ARCH})"
echo

# ---------------------------------------------------------------------------
# Set up build dirs.
# ---------------------------------------------------------------------------
BUILD_ROOT="${KERNEL_DIR}/build"
SRC_CACHE="${BUILD_ROOT}/src-cache"
BUILD_DIR="${BUILD_ROOT}/${TARGET_ARCH}"
OUT_DIR="${BUILD_ROOT}"
mkdir -p "${SRC_CACHE}" "${BUILD_DIR}" "${OUT_DIR}"

TARBALL="linux-${LINUX_VERSION}.tar.xz"
TARBALL_PATH="${SRC_CACHE}/${TARBALL}"
SRC_URL="https://cdn.kernel.org/pub/linux/kernel/v6.x/${TARBALL}"
SRC_DIR="${BUILD_DIR}/linux-${LINUX_VERSION}"

# ---------------------------------------------------------------------------
# Fetch & verify the Linux tarball. Cache locally so repeat builds are fast.
# ---------------------------------------------------------------------------
if [[ ! -f "${TARBALL_PATH}" ]]; then
    echo "fetching ${SRC_URL}"
    wget -q --show-progress -O "${TARBALL_PATH}.tmp" "${SRC_URL}"
    mv "${TARBALL_PATH}.tmp" "${TARBALL_PATH}"
fi

echo "verifying sha256..."
ACTUAL_SHA="$(sha256sum "${TARBALL_PATH}" | cut -d' ' -f1)"
if [[ "${ACTUAL_SHA}" != "${LINUX_SHA256}" ]]; then
    echo "sha256 mismatch for ${TARBALL}:" >&2
    echo "  expected: ${LINUX_SHA256}" >&2
    echo "  actual:   ${ACTUAL_SHA}" >&2
    exit 3
fi

# ---------------------------------------------------------------------------
# Extract (idempotent — skip if the source tree is already present).
# ---------------------------------------------------------------------------
if [[ ! -d "${SRC_DIR}" ]]; then
    echo "extracting ${TARBALL} -> ${BUILD_DIR}/"
    tar -xJf "${TARBALL_PATH}" -C "${BUILD_DIR}/"
fi

cd "${SRC_DIR}"

# ---------------------------------------------------------------------------
# Generate .config:
#   1. Start from Linux's kvm_guest.config baseline (handles all the virt
#      basics for the target arch).
#   2. Merge kernel/config/smolvm.fragment on top.
#   3. Run olddefconfig to resolve any new symbols.
#
# This is the "seed fragment" flow. A follow-up PR will check in the full
# expanded configs and switch this step to copy them verbatim.
# ---------------------------------------------------------------------------
echo "generating .config for ARCH=${KERNEL_ARCH}"
make ARCH="${KERNEL_ARCH}" kvm_guest.config >/dev/null
./scripts/kconfig/merge_config.sh -m -O . .config "${KERNEL_DIR}/config/smolvm.fragment"
make ARCH="${KERNEL_ARCH}" olddefconfig >/dev/null

# ---------------------------------------------------------------------------
# Sanity-check the critical invariants. If any of these fire, the resulting
# kernel would silently fall back to the bug we're trying to kill.
# ---------------------------------------------------------------------------
echo "asserting config invariants..."
assert_yes() {
    if ! grep -q "^$1=y$" .config; then
        echo "  FAIL: $1 is not =y" >&2
        exit 4
    fi
}
assert_no() {
    if grep -q "^$1=" .config; then
        echo "  FAIL: $1 is set (must be unset)" >&2
        exit 4
    fi
}
assert_yes CONFIG_VIRTIO_FS
assert_yes CONFIG_FUSE_FS
assert_yes CONFIG_OVERLAY_FS
assert_yes CONFIG_VIRTIO_BLK
assert_yes CONFIG_VIRTIO_NET
assert_yes CONFIG_VIRTIO_CONSOLE
assert_yes CONFIG_VIRTIO_PCI
assert_yes CONFIG_VIRTIO_MMIO
assert_yes CONFIG_EXT4_FS
assert_no  CONFIG_MODULES
assert_no  CONFIG_NET_9P
assert_no  CONFIG_9P_FS
echo "  ok"

# ---------------------------------------------------------------------------
# Build.
# ---------------------------------------------------------------------------
NPROC="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"
echo "building kernel with -j${NPROC}..."
make ARCH="${KERNEL_ARCH}" -j"${NPROC}"

if [[ ! -f "${KERNEL_IMAGE_PATH}" ]]; then
    echo "expected kernel image not found: ${KERNEL_IMAGE_PATH}" >&2
    exit 5
fi

# ---------------------------------------------------------------------------
# Package the release artifact.
# ---------------------------------------------------------------------------
STAGING="${BUILD_DIR}/staging"
rm -rf "${STAGING}"
mkdir -p "${STAGING}"
cp "${KERNEL_IMAGE_PATH}" "${STAGING}/${KERNEL_IMAGE_NAME}"
cp .config                "${STAGING}/config"
cp "${KERNEL_DIR}/LINUX_VERSION" "${STAGING}/LINUX_VERSION"
(cd "${STAGING}" && sha256sum "${KERNEL_IMAGE_NAME}" config LINUX_VERSION > SHA256SUMS)

ARTIFACT="${OUT_DIR}/smolvm-kernel-v${VERSION}-${TARGET_ARCH}.tar.zst"
echo "packaging -> ${ARTIFACT}"
tar --zstd \
    --sort=name \
    --mtime="@${SOURCE_DATE_EPOCH:-1767225600}" \
    --owner=0 --group=0 --numeric-owner \
    -cf "${ARTIFACT}" \
    -C "${STAGING}" \
    "${KERNEL_IMAGE_NAME}" config SHA256SUMS LINUX_VERSION

echo
echo "=== done ==="
echo "artifact : ${ARTIFACT}"
echo "size     : $(du -h "${ARTIFACT}" | cut -f1)"
echo "sha256   : $(sha256sum "${ARTIFACT}" | cut -d' ' -f1)"
