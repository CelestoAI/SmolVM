#!/bin/sh
# Test image only: run an ordinary application without SSH or a guest agent.
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev 2>/dev/null || true
mount -o remount,rw /
ip link set lo up
ip link set eth0 up
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        ip=*)
            fields=${arg#ip=}
            address=$(echo "$fields" | cut -d: -f1)
            gateway=$(echo "$fields" | cut -d: -f3)
            # Private TAP and slirp both use a directly reachable gateway.
            ip addr add "$address/16" dev eth0
            ip route add default via "$gateway" dev eth0
            ;;
    esac
done
exec /usr/bin/python3 /qemu-policy-app.py
