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
#   2. starts the vsock guest-agent control plane
#   3. brings up loopback + eth0 (DHCP-style static IP from kernel cmdline)
#   4. generates a lightweight SSH host key on first boot
#   5. injects the launching user's pubkey from the kernel cmdline param
#      smolvm.authorized_key_b64=<base64> (matches openclaw's mechanism)
#   6. starts sshd
#   7. parks PID 1 in a sleep loop, signal-handling Firecracker shutdown
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

# ── Timestamp helpers (for host-side startup profiling) ──────
ts_uptime() {
    cut -d' ' -f1 /proc/uptime 2>/dev/null || echo "0.00"
}

ts_epoch() {
    date +%s 2>/dev/null || echo "0"
}

log_ts() {
    STAGE="$1"
    EPOCH="$(ts_epoch)"
    UPTIME="$(ts_uptime)"
    LINE="SMOLVM_TS stage=${STAGE} epoch_s=${EPOCH} uptime_s=${UPTIME}"
    echo "$LINE"
    if [ -d /run ]; then
        mkdir -p /run/smolvm 2>/dev/null || true
        printf '{"stage":"%s","epoch_s":%s,"uptime_s":%s}\n' "$STAGE" "$EPOCH" "$UPTIME" >> /run/smolvm/milestones.jsonl 2>/dev/null || true
        printf '{"stage":"%s","epoch_s":%s,"uptime_s":%s}\n' "$STAGE" "$EPOCH" "$UPTIME" >> /run/smolvm/boot-milestones.jsonl 2>/dev/null || true
    fi
    if [ -d /var/log ]; then
        echo "$LINE" >> /var/log/smolvm-boot.log 2>/dev/null || true
    fi
}

log_ts "init-start"

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

log_ts "mounts-ready"

echo 0 > /proc/sys/kernel/ctrl-alt-del
mount -o remount,rw /
mkdir -p /run/sshd /var/log /tmp
chmod 1777 /tmp

log_ts "root-ready"

# ── Guest agent (vsock control plane) ────────────────────────
# Started before networking and sshd, so explicit-vsock sandboxes can become
# ready without waiting for SSH host-key generation or network setup. SSH-only
# sandboxes can still boot if the agent is missing, but vsock sandboxes require
# the Rust agent to answer.
# Mirrors _base_init_script() in src/smolvm/images/builder.py.
log_ts "guest-agent-start"
if [ -x /usr/local/bin/smolvm-guest-agent ]; then
    /usr/local/bin/smolvm-guest-agent --listen vsock://1024 >/var/log/smolvm-agent.log 2>&1 &
    echo "SmolVM init: guest agent started (PID=$!)"
else
    echo "SmolVM init: guest agent not found; vsock control will be unavailable" >&2
fi
log_ts "guest-agent-started"

# ── Networking ───────────────────────────────────────────────
log_ts "net-config-start"
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
GUEST_MANAGED=$(cat /proc/cmdline | tr ' ' '\n' | grep '^smolvm.network=guest' | head -1)

configure_guest_managed_network() {
    ip link set lo up

    # A custom hook is the authoritative static/DHCP configuration supplied
    # inside the image. The interface name is passed as its first argument.
    if [ -x /etc/smolvm/network.sh ]; then
        /etc/smolvm/network.sh eth0
        return $?
    fi

    # Respect a conventional interfaces file when this image ships ifup.
    if command -v ifup >/dev/null 2>&1 \
        && grep -Eq '^[[:space:]]*iface[[:space:]]+eth0' /etc/network/interfaces 2>/dev/null \
        && ifup eth0; then
        return
    fi

    # SmolVM-provided images use guest-side DHCP when no static hook exists.
    ip link set eth0 up 2>/dev/null || true
    if command -v udhcpc >/dev/null 2>&1 && udhcpc -q -n -t 5 -i eth0; then
        return
    fi
    if command -v dhclient >/dev/null 2>&1 && dhclient -1 eth0; then
        return
    fi

    echo "SmolVM init: eth0 has no guest network configuration; add /etc/smolvm/network.sh" >&2
    return 1
}

if [ -n "$GUEST_MANAGED" ]; then
    if configure_guest_managed_network; then
        log_ts "net-ready"
    else
        log_ts "net-config-failed"
    fi
    hostname smolvm
    log_ts "net-config-done"
else
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
    log_ts "net-config-done"
    log_ts "net-ready"
fi

# ── SSH host keys ────────────────────────────────────────────
log_ts "ssh-hostkey-check-start"
if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then
    ssh-keygen -t ed25519 -f /etc/ssh/ssh_host_ed25519_key -N "" -q 2>/dev/null
fi
log_ts "ssh-hostkey-check-done"

# ── Pubkey injection from kernel cmdline ─────────────────────
# Format: smolvm.authorized_key_b64=<base64-of-the-pubkey-line>.
# Same mechanism as openclaw — published images don't bake keys at
# build time, so each VM gets the launching user's key.
log_ts "ssh-authkey-inject-start"
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
log_ts "ssh-authkey-inject-done"

# ── Clock sync (host-sleep drift) ────────────────────────────
# The guest's clocksource (the TSC under HVF/QEMU) stops advancing
# while the host is asleep, so on wake the system clock lags by the
# sleep duration (issue #330). With no NTP daemon in the sandbox, we
# periodically re-read the emulated hardware RTC — which QEMU keeps
# pinned to host wall-clock time (-rtc clock=host) — and step the
# system clock to match. No-ops on backends with no RTC (Firecracker).
# Mirrors _base_init_script() in src/smolvm/images/builder.py.
log_ts "clock-sync-start"
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
    log_ts "clock-sync-started"
else
    log_ts "clock-sync-disabled"
fi

log_ts "sshd-start"
/usr/sbin/sshd -e &
log_ts "sshd-invoked"

echo "SmolVM init complete: IP=${GUEST_IP}, SSH listening on port 22"
log_ts "init-complete"

# ── Keep PID 1 alive ────────────────────────────────────────
# Use `wait` so signals are delivered promptly (plain `sleep` in a
# while-loop blocks signal delivery until sleep finishes).
while true; do
    sleep 3600 &
    wait $!
done
