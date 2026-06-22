# Copyright 2026 Celesto AI
# Licensed under the Apache License, Version 2.0

"""SmolVM-in-SmolVM preset.

On Linux hosts with KVM nested virtualization enabled, the inner VM runs
Firecracker for near-native speed. On macOS hosts (HVF doesn't expose KVM
to guests), the inner VM falls back to QEMU TCG (software emulation).
"""

from __future__ import annotations

from smolvm.presets._types import Preset

_SETUP = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Try to surface /dev/kvm (Linux nested KVM path).
# On macOS hosts HVF does not expose KVM to guests, so this is a no-op there.
if [ ! -e /dev/kvm ]; then
    modprobe kvm 2>/dev/null || true
    modprobe kvm_intel 2>/dev/null || true
    modprobe kvm_amd 2>/dev/null || true
fi
if [ -e /dev/kvm ]; then
    chmod 666 /dev/kvm
else
    echo "Note: /dev/kvm not present — inner VMs will use QEMU TCG (software emulation)."
    echo "For hardware-accelerated inner VMs, use a Linux host with KVM nested virt enabled."
fi

# Refresh package index now so _INSTALL can fire the actual installs in
# the background while pip runs in the foreground — cutting total wall time
# roughly in half by overlapping the two slowest operations.
apt-get update -q -y
"""

_INSTALL = r"""
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

ARCH=$(uname -m)
case "$ARCH" in
    aarch64) QEMU_PKG="qemu-system-arm" ;;
    x86_64)  QEMU_PKG="qemu-system-x86" ;;
    *)       QEMU_PKG="qemu-system-arm qemu-system-x86" ;;
esac

# python3-venv must be present before we can run `python3 -m venv`, so
# install it synchronously first. It's tiny (~2 MB) and often already there.
apt-get install -y -q --no-install-recommends python3 python3-venv

# QEMU is large and independent of pip — install it in the background while
# pip installs SmolVM in the foreground. dpkg lock is already free because
# the python3-venv install above has finished.
apt-get install -y -q --no-install-recommends \
    ${QEMU_PKG} qemu-utils ca-certificates curl &
APT_PID=$!

# Install SmolVM in the foreground (overlaps with QEMU apt above).
python3 -m venv /opt/smolvm-venv
/opt/smolvm-venv/bin/pip install --upgrade pip -q
/opt/smolvm-venv/bin/pip install smolvm -q
ln -sf /opt/smolvm-venv/bin/smolvm /usr/local/bin/smolvm

# Wait for the QEMU install before we check for binaries below.
wait $APT_PID

# Install Firecracker only when KVM is available — it won't run without it.
if [ -e /dev/kvm ]; then
    case "$ARCH" in
        x86_64)  FC_ARCH=x86_64 ;;
        aarch64) FC_ARCH=aarch64 ;;
        *) FC_ARCH="" ;;
    esac
    if [ -n "${FC_ARCH:-}" ]; then
        FC_VERSION=v1.10.1
        curl -fsSL -o /tmp/fc.tgz \
            "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${FC_ARCH}.tgz"
        tar -xzf /tmp/fc.tgz -C /tmp
        FC_DIR="/tmp/release-${FC_VERSION}-${FC_ARCH}"
        install -m 0755 "${FC_DIR}/firecracker-${FC_VERSION}-${FC_ARCH}" \
            /usr/local/bin/firecracker
        install -m 0755 "${FC_DIR}/jailer-${FC_VERSION}-${FC_ARCH}" \
            /usr/local/bin/jailer
        rm -rf /tmp/fc.tgz "${FC_DIR}"
    fi
fi

if [ -e /dev/kvm ] && command -v firecracker >/dev/null 2>&1; then
    INNER_BACKEND=firecracker
else
    INNER_BACKEND=qemu
fi

cat >/etc/profile.d/smolvm-inner.sh <<EOF
export SMOLVM_DEFAULT_BACKEND=${INNER_BACKEND}
EOF

if [ "$INNER_BACKEND" = "firecracker" ]; then
    echo "Ready (KVM). Inner VM:  smolvm sandbox start --backend firecracker"
else
    echo "Ready (TCG). Inner VM:  smolvm sandbox start --backend qemu"
fi
"""

SMOLVM_PRESET = Preset(
    name="smolvm",
    aliases=("smolvm-in-smolvm",),
    summary="Sandbox preinstalled with SmolVM (uses Firecracker with KVM, QEMU TCG otherwise).",
    setup_script=_SETUP,
    install_script=_INSTALL,
    default_mem_mib=8192,
    default_disk_mib=20480,
    launch_command="bash -l",
)
