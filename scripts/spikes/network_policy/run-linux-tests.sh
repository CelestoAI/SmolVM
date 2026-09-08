#!/usr/bin/env bash
# Runs only in a disposable Docker network namespace. Never use --network host,
# --privileged, or host filesystem mounts for these tests.
set -euo pipefail
cd "$(dirname "$0")"
docker build --build-context policy=../../../src/smolvm/network_policy -t smolvm-network-policy-spike .
docker build --build-context policy=../../../src/smolvm/network_policy -f Dockerfile.linux -t smolvm-network-policy-linux .
exec docker run --rm --network none \
  --cap-drop ALL --cap-add NET_ADMIN --cap-add SETUID --cap-add SETGID \
  --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add SETPCAP --cap-add KILL \
  --device /dev/net/tun --security-opt no-new-privileges \
  --sysctl net.ipv4.ip_forward=1 \
  --sysctl net.ipv4.conf.default.rp_filter=0 \
  --sysctl net.ipv4.conf.all.rp_filter=0 \
  --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --memory 512m --cpus 2 -e SMOLVM_POLICY_LINUX_TESTS=1 \
  smolvm-network-policy-linux
