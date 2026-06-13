#!/bin/sh
# SmolVM PID 1 init for layered presets (codex, claude-code, hermes, pi).
#
# These presets boot off a generic Ubuntu rootfs that doesn't have a
# preset-specific /init like openclaw does. Rather than running systemd
# (which adds 5–15s of boot overhead and isn't needed for a sandbox VM
# whose only job is to host an agent CLI behind SSH), we replace it with
# this minimal script that:
#
#   1. mounts the essential virtual filesystems
#   2. brings up loopback + eth0 (DHCP-style static IP from kernel cmdline)
#   3. generates SSH host keys on first boot
#   4. injects the launching user's pubkey from the kernel cmdline param
#      smolvm.authorized_key_b64=<base64> (matches openclaw's mechanism)
#   5. starts sshd
#   6. parks PID 1 in a sleep loop, signal-handling Firecracker shutdown
#
# Mirrors `_base_init_script()` in src/smolvm/images/builder.py — keep
# them in sync if either changes.

set -u

# ── Signal handling ──────────────────────────────────────────
# Firecracker's SendCtrlAltDel sends Ctrl+Alt+Del to the guest kernel.
# Default kernel response is hardware reboot (not supported in
# Firecracker → VM hangs). We disable CAD so the kernel sends SIGINT to
# PID 1 instead, where we trap it.
shutdown() {
    echo "SmolVM init: shutting down..."
    kill -TERM -1 2>/dev/null
    sleep 0.2
    sync
    poweroff -f
}
trap shutdown INT TERM

# ── Mount essential filesystems ──────────────────────────────
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev 2>/dev/null   # may already be mounted
mkdir -p /dev/pts
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /run
# Keep /tmp on the root disk, not tmpfs. Package managers use /tmp for
# temporary writes, and a memory-sized tmpfs can fill up even when the disk
# still has plenty of space.

echo 0 > /proc/sys/kernel/ctrl-alt-del
mount -o remount,rw /
mkdir -p /run/sshd /var/log /tmp
chmod 1777 /tmp

# ── Guest agent (vsock control plane) ────────────────────────
# Started before networking and sshd, so explicit-vsock sandboxes can become
# ready without waiting for SSH host-key generation or network setup. Skipped
# silently if the agent is missing (the host falls back to SSH in that case).
# Mirrors _base_init_script() in src/smolvm/images/builder.py.
if [ -x /usr/local/bin/smolvm-guest-agent ]; then
    /usr/local/bin/smolvm-guest-agent --listen vsock://1024 >/var/log/smolvm-agent.log 2>&1 &
    echo "SmolVM init: guest agent started (PID=$!)"
else
    echo "SmolVM init: guest agent not found, continuing without it" >&2
fi

# ── Networking ───────────────────────────────────────────────
# Format: ip=<guest_ip>::<gateway>:<netmask>::eth0:off (kernel ip= param)
netmask_to_prefix() {
    IFS=.
    set -- $1
    IFS=' '

    [ $# -eq 4 ] || return 1

    PREFIX=0
    ZERO_SEEN=0
    for OCTET in "$@"; do
        case "$OCTET" in
            255) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 8)) ;;
            254) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 7)); ZERO_SEEN=1 ;;
            252) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 6)); ZERO_SEEN=1 ;;
            248) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 5)); ZERO_SEEN=1 ;;
            240) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 4)); ZERO_SEEN=1 ;;
            224) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 3)); ZERO_SEEN=1 ;;
            192) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 2)); ZERO_SEEN=1 ;;
            128) [ "$ZERO_SEEN" -eq 0 ] || return 1; PREFIX=$((PREFIX + 1)); ZERO_SEEN=1 ;;
            0) ZERO_SEEN=1 ;;
            *) return 1 ;;
        esac
    done

    echo "$PREFIX"
}

IP_CONFIG=$(cat /proc/cmdline | tr ' ' '\n' | grep '^ip=' | head -1)
if [ -n "$IP_CONFIG" ]; then
    IP_FIELDS=$(echo "$IP_CONFIG" | cut -d= -f2-)
    GUEST_IP=$(echo "$IP_FIELDS" | cut -d: -f1)
    GATEWAY=$(echo "$IP_FIELDS" | cut -d: -f3)
    NETMASK=$(echo "$IP_FIELDS" | cut -d: -f4)
else
    GUEST_IP="172.16.0.2"
    GATEWAY="172.16.0.1"
    NETMASK="255.255.255.0"
fi

PREFIX=$(netmask_to_prefix "$NETMASK") || PREFIX=24

ip link set lo up
ip link set eth0 up 2>/dev/null || true
ip addr add "${GUEST_IP}/${PREFIX}" dev eth0 2>/dev/null || true
ip route add default via "${GATEWAY}" dev eth0 2>/dev/null || true

if [ -n "$GATEWAY" ]; then
    echo "nameserver ${GATEWAY}" > /etc/resolv.conf
    echo "nameserver 8.8.8.8" >> /etc/resolv.conf
    echo "nameserver 8.8.4.4" >> /etc/resolv.conf
else
    echo "nameserver 8.8.8.8" > /etc/resolv.conf
    echo "nameserver 8.8.4.4" >> /etc/resolv.conf
fi

hostname smolvm

# ── SSH host keys ────────────────────────────────────────────
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
    ssh-keygen -A 2>/dev/null
fi

# ── Pubkey injection from kernel cmdline ─────────────────────
# Format: smolvm.authorized_key_b64=<base64-of-the-pubkey-line>.
# Same mechanism as openclaw — published images don't bake keys at
# build time, so each VM gets the launching user's key.
AUTHKEY_B64=$(cat /proc/cmdline | tr ' ' '\n' \
    | grep '^smolvm\.authorized_key_b64=' | head -1 | cut -d= -f2-)
if [ -n "$AUTHKEY_B64" ]; then
    DECODED=$(echo "$AUTHKEY_B64" | base64 -d 2>/dev/null)
    if [ -n "$DECODED" ]; then
        mkdir -p /root/.ssh
        chmod 700 /root/.ssh
        echo "$DECODED" > /root/.ssh/authorized_keys
        chmod 600 /root/.ssh/authorized_keys
    fi
fi

# ── Clock sync (host-sleep drift) ────────────────────────────
# The guest's clocksource (the TSC under HVF/QEMU) stops advancing
# while the host is asleep, so on wake the system clock lags by the
# sleep duration (issue #330). With no NTP daemon in the sandbox, we
# periodically re-read the emulated hardware RTC — which QEMU keeps
# pinned to host wall-clock time (-rtc clock=host) — and step the
# system clock to match. No-ops on backends with no RTC (Firecracker).
# Mirrors _base_init_script() in src/smolvm/images/builder.py.
HWCLOCK=""
for cand in hwclock /usr/sbin/hwclock /sbin/hwclock; do
    if HWCLOCK_PATH=$(command -v "$cand" 2>/dev/null); then
        HWCLOCK="$HWCLOCK_PATH"
        break
    fi
done
if [ -n "$HWCLOCK" ]; then
    (
        while true; do
            "$HWCLOCK" -s -u 2>/dev/null || true
            sleep 30
        done
    ) &
    echo "SmolVM init: clock-sync loop started (PID=$!)"
fi

/usr/sbin/sshd -e &

echo "SmolVM init complete: IP=${GUEST_IP}, SSH listening on port 22"

# ── Keep PID 1 alive ────────────────────────────────────────
# Use `wait` so signals are delivered promptly (plain `sleep` in a
# while-loop blocks signal delivery until sleep finishes).
while true; do
    sleep 3600 &
    wait $!
done
