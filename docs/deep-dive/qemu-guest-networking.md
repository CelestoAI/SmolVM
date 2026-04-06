# QEMU Guest Networking

## The Big Picture

When a QEMU-backed VM boots, it needs three things to reach the internet: an **IP address**, a **gateway**, and a **DNS server**. SmolVM uses QEMU's built-in "user-mode" networking (SLIRP) which handles all of this without requiring root or host-side TAP devices — but it has quirks that affect how the guest discovers these settings.

---

## How SLIRP Works

SLIRP creates a virtual network inside the QEMU process. The guest sees a normal network interface, but traffic is actually proxied through the host's TCP/IP stack. No packets ever hit a real network interface on the host.

The fixed addresses are:

| Role       | Address     |
|------------|-------------|
| Guest IP   | 10.0.2.15   |
| Gateway    | 10.0.2.2    |
| DNS server | 10.0.2.3    |

SLIRP includes a tiny DHCP server that advertises these to the guest. It also includes a DNS forwarder at 10.0.2.3 that proxies queries to the host's real DNS.

SmolVM passes `dns=10.0.2.3` explicitly in the `-netdev user` arguments to make sure the DHCP response includes the DNS server:

```
-netdev user,id=net0,dns=10.0.2.3,hostfwd=tcp:127.0.0.1:2200-:22
```

The `hostfwd` rule is how SSH reaches the guest — it maps a host port to guest port 22.

---

## Why Ubuntu Needs Special Treatment

Alpine and Debian images are built by SmolVM with `nameserver 8.8.8.8` hardcoded in `/etc/resolv.conf`. DNS just works from the first moment.

Ubuntu cloud images are different. They use **cloud-init** and **systemd-resolved** for network configuration. The boot sequence is:

1. Kernel boots, systemd starts
2. `systemd-networkd` configures the interface (cloud-init generates a static netplan config)
3. `systemd-resolved` starts — but has **no upstream DNS servers** because the static netplan config doesn't include any
4. `systemd-timesyncd` tries to sync the clock via NTP — but can't resolve the NTP pool hostname
5. SSH becomes available — the guest has no working DNS and a stale clock

Cloud-init generates the netplan config from the seed ISO metadata. Without an explicit `nameservers` block in that config, `systemd-resolved` starts with an empty upstream list.

---

## The Fix: cloud-init bootcmd

SmolVM uses cloud-init's `bootcmd` directive to fix DNS and clock before SSH starts. `bootcmd` runs very early in boot — before `write_files` and long before `runcmd`.

```yaml
bootcmd:
  # 1. Configure DNS
  - mkdir -p /etc/systemd/resolved.conf.d
  - ['sh', '-c', 'printf "[Resolve]\nDNS=10.0.2.3\n" > /etc/systemd/resolved.conf.d/smolvm-dns.conf']
  - ['systemctl', 'restart', 'systemd-resolved']
  # 2. Sync the clock (NTP now works because DNS works)
  - ['systemctl', 'restart', 'systemd-timesyncd']
```

Why `bootcmd` and not `runcmd`? Because `runcmd` runs in cloud-init's "final" stage — after SSH is already available. A user who SSHs in immediately would still see broken DNS. `bootcmd` runs before any of that.

---

## Firecracker Is Different

Firecracker VMs don't use SLIRP. They get real TAP devices on the host with nftables NAT rules for outbound traffic. DNS and clock are not an issue because:

- The custom-built Alpine/Debian images have hardcoded DNS in `/etc/resolv.conf`
- The kernel reads the host clock directly at boot (no cloud image timestamp to override it)

The domain allowlist feature (`internet_settings.allowed_domains`) works by resolving domains to IPs and applying nftables egress rules on the TAP device. This only works with Firecracker — SLIRP traffic bypasses host nftables entirely.

---

## Seed ISO Caching

The cloud-init seed ISO (containing user-data, meta-data) is cached on disk to avoid rebuilding it every time. The cache key is a hash of:

- SSH public key
- Instance ID
- Hostname
- User-data content

Including user-data in the hash ensures that changes to the cloud-init template (like adding `bootcmd`) automatically invalidate stale cached ISOs.
