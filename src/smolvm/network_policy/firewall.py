"""Per-sandbox nftables ownership and fail-closed listener admission.

Install before guest execution. Removing these rules is only safe after the VM
has stopped. Callers serialize lifecycle changes for the same sandbox; separate
sandboxes never rewrite each other's tables.
"""

import hashlib
import ipaddress
import json
import os
import re
import subprocess
from dataclasses import dataclass

_PROHIBITED = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.31.196.0/24",
    "192.52.193.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "192.175.48.0/24",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


def _nft(*args: str, script: str | None = None) -> str:
    command = ["nft", *args]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    return subprocess.run(
        command, input=script, check=True, capture_output=True, text=True, timeout=5
    ).stdout


@dataclass(frozen=True)
class NetworkBinding:
    """Trusted host placement, never derived from guest request headers."""

    tap: str
    guest_ip: str
    gateway_ip: str
    host_addresses: tuple[str, ...]
    resolver_addresses: tuple[str, ...] = ()
    proxy_uid: int | None = None
    proxy_port: int | None = None
    platform_ports: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        for field in ("host_addresses", "resolver_addresses", "platform_ports"):
            object.__setattr__(self, field, tuple(getattr(self, field)))
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", self.tap) is None:
            raise ValueError("Invalid network interface name.")
        for address in (
            self.guest_ip,
            self.gateway_ip,
            *self.host_addresses,
            *self.resolver_addresses,
        ):
            if str(ipaddress.IPv4Address(address)) != address:
                raise ValueError("Network addresses must be canonical IPv4 addresses.")
        if self.gateway_ip not in self.host_addresses or self.guest_ip == self.gateway_ip:
            raise ValueError("The gateway must be included in the host address inventory.")
        if (self.proxy_uid is None) != (self.proxy_port is None):
            raise ValueError("A proxy requires both its process identity and listener port.")
        if self.proxy_uid is not None and not 60000 <= self.proxy_uid < 2**32 - 1:
            raise ValueError("A proxy requires an isolated process identity.")
        ports = self.platform_ports + (() if self.proxy_port is None else (self.proxy_port,))
        if any(type(port) is not int or not 1 <= port <= 65535 for port in ports):
            raise ValueError("Network ports must be integers between 1 and 65535.")
        if self.proxy_port is not None and self.proxy_port in self.platform_ports:
            raise ValueError("The workload proxy cannot also be a platform exception.")

    @property
    def table(self) -> str:
        return "smolvm_np_" + hashlib.sha256(self.tap.encode()).hexdigest()[:16]

    def rules(self) -> str:
        """Render closed admission; each replacement is one nft transaction."""
        networks = [ipaddress.IPv4Network(value) for value in _PROHIBITED]
        networks.extend(ipaddress.IPv4Network(value + "/32") for value in self.host_addresses)
        prohibited = ", ".join(str(value) for value in ipaddress.collapse_addresses(networks))
        platform = ""
        if self.platform_ports:
            ports = ", ".join(str(port) for port in sorted(set(self.platform_ports)))
            platform = f"ip daddr {self.gateway_ip} tcp dport {{ {ports} }} accept"
        output = ""
        if self.proxy_uid is not None:
            dns = ""
            if self.resolver_addresses:
                resolvers = ", ".join(sorted(set(self.resolver_addresses)))
                dns = (
                    f"ip daddr {{ {resolvers} }} meta l4proto {{ tcp, udp }} "
                    "th dport 53 counter name proxy_allowed accept"
                )
            reply_rule = (
                f"ip daddr {{ {self.guest_ip}, {self.gateway_ip} }} tcp sport {self.proxy_port} "
                "ct direction reply ct state established accept"
            )
            output = f"""
    chain proxy_output {{
        meta nfproto ipv6 counter name proxy_drops drop
        {reply_rule}
        {dns}
        fib daddr type local counter name proxy_drops drop
        ip daddr @prohibited_v4 counter name proxy_drops drop
        tcp dport {{ 80, 443 }} counter name proxy_allowed accept
        counter name proxy_drops drop
    }}
    chain output {{
        type filter hook output priority -150; policy accept;
        meta skuid {self.proxy_uid} jump proxy_output
    }}"""
        return f"""table inet {self.table} {{
    counter guest_drops {{}}
    counter proxy_drops {{}}
    counter proxy_allowed {{}}
    set admitted_ports {{ type inet_service; flags timeout; timeout 3s; }}
    set prohibited_v4 {{ type ipv4_addr; flags interval; elements = {{ {prohibited} }} }}
    chain guest_input {{
        meta nfproto ipv6 counter name guest_drops drop
        ip saddr != {self.guest_ip} counter name guest_drops drop
        {platform}
        ip daddr {self.gateway_ip} tcp dport @admitted_ports accept
        ct state established ct direction reply accept
        counter name guest_drops drop
    }}
    chain input {{
        type filter hook input priority -150; policy accept;
        iifname "{self.tap}" jump guest_input
    }}
    chain forward {{
        type filter hook forward priority -150; policy accept;
        iifname "{self.tap}" meta nfproto ipv6 counter name guest_drops drop
        iifname "{self.tap}" ip saddr != {self.guest_ip} counter name guest_drops drop
        iifname "{self.tap}" ct state established ct direction reply accept
        iifname "{self.tap}" counter name guest_drops drop
    }}
    {output}
}}
"""

    def fingerprint(self) -> str:
        """Identify effective rules, excluding counters and lease countdowns."""
        data = json.loads(_nft("-j", "-n", "list", "table", "inet", self.table))

        def stable(value):
            if isinstance(value, dict):
                return {
                    key: stable(item)
                    for key, item in value.items()
                    if key not in {"handle", "packets", "bytes", "expires"}
                }
            if isinstance(value, list):
                return [
                    stable(item)
                    for item in value
                    if not isinstance(item, dict) or "metainfo" not in item
                ]
            return value

        return hashlib.sha256(json.dumps(stable(data), sort_keys=True).encode()).hexdigest()

    def install(self) -> None:
        """Atomically install/rebuild only this sandbox's table, admission closed."""
        tables = json.loads(_nft("-j", "list", "tables"))["nftables"]
        exists = any(
            item.get("table", {}).get("family") == "inet"
            and item.get("table", {}).get("name") == self.table
            for item in tables
        )
        delete = f"delete table inet {self.table}\n" if exists else ""
        _nft("-f", "-", script=delete + self.rules())

    def admit(self) -> None:
        """Called only after worker identity, credentials and readiness verify."""
        if self.proxy_port is None:
            raise ValueError("A deny-all sandbox has no workload proxy to admit.")
        _nft("add", "element", "inet", self.table, "admitted_ports", f"{{ {self.proxy_port} }}")

    def renew_admission(self) -> None:
        """Refresh atomically; an already-expired admission must not reopen."""
        if self.proxy_port is None:
            raise ValueError("Deny-all has no admission lease.")
        element = f"inet {self.table} admitted_ports {{ {self.proxy_port} }}"
        _nft("-f", "-", script=f"delete element {element}\nadd element {element}\n")

    def fence(self) -> None:
        """An error here requires stopping/quarantining the VM, not continuing."""
        _nft("flush", "set", "inet", self.table, "admitted_ports")

    def remove(self) -> None:
        """Remove owned rules AFTER the VM and worker have stopped."""
        _nft("delete", "table", "inet", self.table)
