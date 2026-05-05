#!/usr/bin/env bash
# Layer a preset on top of the shared base rootfs.
#
# Usage:  build-preset.sh <preset> <base-rootfs.ext4> <output-dir> [size-mb]
# Env:    OS=ubuntu|alpine (default: ubuntu)
#         ARCH=amd64|arm64 (default: dpkg/uname)
#
# Produces:
#   ubuntu: <output-dir>/<preset>-<arch>-rootfs.ext4
#   alpine: <output-dir>/<preset>-<arch>-alpine-rootfs.ext4
#
# The Ubuntu asset name has no OS suffix for backward compat with the
# existing release; Alpine introduces the suffix as it lands. Same naming
# is mirrored in the published manifest's URL builder.
#
# Strategy: copy the base ext4, mount it, chroot into it, run the
# preset-specific install script, unmount. The result is a self-contained
# ext4 ready for zstd compression and upload.
#
# NOTE: openclaw uses its own builder (build_openclaw_rootfs) which bakes
# in a custom init script, sidecars, and systemctl proxy. It's not layered
# through this script. This script handles: codex, claude-code, hermes, pi.
#
# Runs in CI on a matching-arch runner. Requires: chroot, mount (loop).
set -euo pipefail

PRESET="${1:?Usage: build-preset.sh <preset> <base-rootfs.ext4> <output-dir> [size-mb]}"
BASE_ROOTFS="${2:?Missing base-rootfs.ext4 path}"
OUT_DIR="${3:?Missing output directory}"
SIZE_MB="${4:-4096}"
OS="${OS:-ubuntu}"
ARCH="${ARCH:-$(dpkg --print-architecture 2>/dev/null || uname -m)}"

# Normalize arch naming
case "$ARCH" in
  x86_64|amd64) ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

case "$OS" in
  ubuntu)
    OUT_NAME="${PRESET}-${ARCH}"
    SHELL_BIN="/bin/bash"
    ;;
  alpine)
    OUT_NAME="${PRESET}-${ARCH}-alpine"
    # Alpine ships /bin/bash (we apk-install it in the base) so the install
    # snippets below — which use ``set -euo pipefail`` and other bash-isms —
    # still run cleanly without busybox-ash quirks.
    SHELL_BIN="/bin/bash"
    ;;
  *)
    echo "Unsupported OS: $OS (expected: ubuntu | alpine)" >&2
    exit 1
    ;;
esac

# Phase 1 of the Alpine rollout (#264) restricts which presets are eligible:
# pure-JS presets only. hermes pulls musllinux-incompatible Python wheels
# and openclaw pulls glibc-only @node-llama-cpp prebuilts.
if [ "$OS" = "alpine" ]; then
  case "$PRESET" in
    codex|claude-code|pi) ;;
    *)
      echo "Preset '$PRESET' is not yet supported on Alpine (Phase 1 covers codex/claude-code/pi)." >&2
      exit 1
      ;;
  esac
fi

mkdir -p "$OUT_DIR"
ROOTFS="$OUT_DIR/${OUT_NAME}-rootfs.ext4"

echo "==> Copying base rootfs for preset '$PRESET' ($ARCH, $OS)..."
cp "$BASE_ROOTFS" "$ROOTFS"

# Resize if needed (base may be smaller than target)
CURRENT_SIZE_MB=$(stat -c '%s' "$ROOTFS" 2>/dev/null || stat -f '%z' "$ROOTFS")
CURRENT_SIZE_MB=$((CURRENT_SIZE_MB / 1048576))
if [ "$SIZE_MB" -gt "$CURRENT_SIZE_MB" ]; then
  echo "==> Resizing from ${CURRENT_SIZE_MB}M to ${SIZE_MB}M..."
  truncate -s "${SIZE_MB}M" "$ROOTFS"
  resize2fs "$ROOTFS" >/dev/null 2>&1
fi

# Mount the ext4 image
MNT=$(mktemp -d)
mount -o loop "$ROOTFS" "$MNT"

cleanup() {
  umount "$MNT/dev/pts" 2>/dev/null || true
  umount "$MNT/dev" 2>/dev/null || true
  umount "$MNT/sys" 2>/dev/null || true
  umount "$MNT/proc" 2>/dev/null || true
  umount "$MNT" 2>/dev/null || true
  rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT

# Bind-mount /proc, /sys, /dev for chroot
mount --bind /proc "$MNT/proc"
mount --bind /sys "$MNT/sys"
mount --bind /dev "$MNT/dev"
mount --bind /dev/pts "$MNT/dev/pts" 2>/dev/null || true

# DNS resolution inside chroot. Save the original so we can restore it
# before unmount — otherwise the CI runner's resolv.conf bakes into the
# published rootfs and ends up on every guest VM.
RESOLV_BACKUP=""
if [ -e "$MNT/etc/resolv.conf" ]; then
  RESOLV_BACKUP=$(mktemp)
  cp -a "$MNT/etc/resolv.conf" "$RESOLV_BACKUP"
fi
cp /etc/resolv.conf "$MNT/etc/resolv.conf" 2>/dev/null || true

echo "==> Installing preset '$PRESET' ($OS)..."

case "$PRESET" in
  codex)
    chroot "$MNT" "$SHELL_BIN" -c '
      set -euo pipefail
      npm install -g --silent @openai/codex
      npm cache clean --force >/dev/null 2>&1 || true
      rm -rf /root/.npm /root/.cache /tmp/*
    '
    ;;

  claude-code)
    chroot "$MNT" "$SHELL_BIN" -c '
      set -euo pipefail
      npm install -g --silent @anthropic-ai/claude-code
      npm cache clean --force >/dev/null 2>&1 || true
      rm -rf /root/.npm /root/.cache /tmp/*
    '
    ;;

  hermes)
    # Ubuntu-only — gated above, but keep the install body here so a
    # future Alpine-compatible spike can flip the gate without rewriting.
    chroot "$MNT" "$SHELL_BIN" -c '
      set -euo pipefail
      if [ ! -d /opt/hermes-agent ]; then
        git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent
      fi
      cd /opt/hermes-agent
      uv venv
      uv pip install -e ".[all]" || uv pip install -e .
      ln -sf /opt/hermes-agent/.venv/bin/hermes /usr/local/bin/hermes
      # uv keeps a wheel cache (~/.cache/uv) — gigabytes for "[all]" extras.
      uv cache clean >/dev/null 2>&1 || true
      # .git is dead weight for a non-developing install.
      rm -rf /opt/hermes-agent/.git
      rm -rf /root/.cache /tmp/*
    '
    ;;

  pi)
    chroot "$MNT" "$SHELL_BIN" -c '
      set -euo pipefail
      npm install -g --silent @mariozechner/pi-coding-agent
      npm cache clean --force >/dev/null 2>&1 || true
      rm -rf /root/.npm /root/.cache /tmp/*
    '
    ;;

  *)
    echo "Unknown preset: $PRESET"
    exit 1
    ;;
esac

# Bake the SmolVM PID 1 init script. CLI boot args (init=/init +
# smolvm.authorized_key_b64=<base64>) are read by this script to install
# the launching user's pubkey into /root/.ssh/authorized_keys at boot.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
install -m 0755 "$SCRIPT_DIR/preset-init.sh" "$MNT/init"

# Restore (or remove) /etc/resolv.conf so the runner's DNS doesn't leak
# into the published rootfs. The init script writes 8.8.8.8 / 8.8.4.4 at
# boot, so removing it on the empty case is safe.
if [ -n "$RESOLV_BACKUP" ]; then
  cp -a "$RESOLV_BACKUP" "$MNT/etc/resolv.conf"
  rm -f "$RESOLV_BACKUP"
else
  rm -f "$MNT/etc/resolv.conf"
fi

echo "==> Preset rootfs: $ROOTFS ($(du -sh "$ROOTFS" | cut -f1))"
