# Network Egress Controls

## The Big Picture

By default, every SmolVM sandbox has unrestricted internet access. The `internet_settings` feature lets you lock down which external domains a VM can reach. This is useful when running untrusted AI agent code — you want the agent to call specific APIs but not exfiltrate data to arbitrary endpoints.

```python
from smolvm import SmolVM

vm = SmolVM(internet_settings={
    "allowed_domains": ["https://api.openai.com", "https://example.com"],
})
```

Only Firecracker VMs support egress controls. QEMU uses user-mode networking (SLIRP) which bypasses host-side firewalls.

---

## How It Works

### 1. Domain Resolution

When a VM is created with `allowed_domains`, SmolVM resolves each domain to IPv4 addresses using `socket.getaddrinfo()`. This happens once at VM creation time.

```
"https://api.openai.com" → ["104.18.6.192", "104.18.7.192"]
"example.com"            → ["93.184.216.34"]
```

URLs are parsed to extract just the hostname. Bare domains work too. The wildcard `"*"` means allow everything (the default).

### 2. nftables Egress Rules

The resolved IPs are passed to `NetworkManager.apply_egress_allowlist()`, which installs per-TAP nftables rules in the SmolVM filter table:

```
# Allow return traffic for established connections
iifname "tap42" ct state established,related counter accept

# Allow traffic to the specific IPs
iifname "tap42" ip daddr { 104.18.6.192, 104.18.7.192, 93.184.216.34 } counter accept

# Drop everything else from this TAP
iifname "tap42" ip daddr != { 104.18.6.192, ... } counter drop
```

Each rule is tagged with a comment like `smolvm:egress:tap42:allow` so it can be cleanly removed when the VM is deleted.

### 3. What Gets Through

| Traffic | Allowed? |
|---------|----------|
| DNS (port 53) | Yes — goes through the host's NAT |
| Allowed domain IPs | Yes |
| Any other outbound IP | Dropped |
| TAP-to-TAP (VM-to-VM) | Always dropped (isolation rule) |

DNS queries still work because they go through the host's NAT masquerade rule, not directly to the internet. The guest resolves a domain, but if the IP isn't in the allowlist, the connection is blocked.

---

## Limitations

**IPs are resolved at creation time.** If a domain's IP changes while the VM is running, the new IP won't be in the allowlist. This is acceptable for short-lived sandbox VMs (minutes to hours), but not for long-running ones.

**CDN domains may need multiple entries.** A domain behind a CDN (like CloudFront) can resolve to many IPs across regions. `getaddrinfo()` returns whatever the host's DNS gives at that moment, which may not cover all edge nodes.

**IPv6 is filtered out.** The nftables rules use `ip daddr` (IPv4 only). IPv6 addresses from AAAA records are silently dropped during resolution. If a guest connects to an IPv6-only host, it won't be in the allowlist.

**HTTP method filtering is not yet enforced.** The `allowed_http_methods` field exists in `InternetSettings` for forward-compatibility, but filtering HTTP methods requires inspecting request payloads — impossible for HTTPS without a MITM proxy.

**QEMU backend is not supported.** SLIRP proxies traffic through the QEMU process, so host-side nftables rules don't see it. A warning is logged if you set `internet_settings` with QEMU.

---

## Cleanup

When a VM is deleted, `remove_egress_rules(tap_device)` deletes all nftables rules matching the `smolvm:egress:{tap_device}:` comment prefix. This happens automatically in the teardown path alongside TAP device removal and NAT rule cleanup.
