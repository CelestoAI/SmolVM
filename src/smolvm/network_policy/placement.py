"""Translate an existing NAT lease into a stable, host-owned policy placement."""

from __future__ import annotations

import grp
import ipaddress
import json
import os
import pwd
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smolvm.types import VMInfo

from .firewall import NetworkBinding
from .setup import require_runtime

# Host-global ownership, including SDKs with separate in-memory inventories.
STATE_DIRECTORY = Path("/run/smolvm/network-policy")


def validate_host(vm: VMInfo, data_dir: Path) -> None:
    """Check before boot; never elevate an arbitrary Python interpreter with sudo."""
    policy = vm.config.network_policy
    if policy is None:
        raise ValueError("The sandbox has no strict network policy.")
    # model_copy deliberately skips Pydantic validation. Recheck persisted/copied
    # configuration at the runtime boundary without requiring old image paths.
    type(vm.config).model_validate(vm.config.model_dump(), context={"validate_paths": False})
    require_runtime(policy)
    if os.geteuid() != 0:
        raise RuntimeError(
            "Strict network policies require a root-owned Linux sandbox service; "
            "run this sandbox through that service."
        )
    private_roots = (
        STATE_DIRECTORY.resolve(),
        data_dir.resolve(),
        Path(tempfile.gettempdir()).resolve(),
    )
    for mount in vm.config.workspace_mounts:
        shared = mount.host_path.resolve()
        if any(root == shared or root.is_relative_to(shared) for root in private_roots):
            raise ValueError(
                f"Sandbox '{vm.vm_id}' shares host policy files; remove that shared folder "
                f"or run 'smolvm sandbox delete {vm.vm_id}'."
            )
        if shared == STATE_DIRECTORY.resolve() or shared.is_relative_to(STATE_DIRECTORY.resolve()):
            raise ValueError("Host network policy files cannot be shared with a sandbox.")
        temporary = Path(tempfile.gettempdir()).resolve()
        if shared.is_relative_to(temporary):
            relative = shared.relative_to(temporary)
            if relative.parts and relative.parts[0].startswith("smolvm-policy-"):
                raise ValueError("Network worker files cannot be shared with a sandbox.")


def placement(vm: VMInfo) -> tuple[NetworkBinding, Path]:
    """Derive identity from the allocated TAP, not an ephemeral listener port.

    The caller must retain the IP lease until verified shutdown and policy
    cleanup. The host-global state lock must be held before installing rules.
    This function discovers placement only; it does not reserve or admit it.
    """
    network = vm.network
    policy = vm.config.network_policy
    if policy is None or network is None or network.mode != "nat":
        raise ValueError("Strict networking requires an allocated NAT lease.")
    address = ipaddress.IPv4Address(network.guest_ip)
    pool = ipaddress.IPv4Network("172.16.0.0/16")
    if address not in pool:
        raise ValueError("Strict networking requires a SmolVM NAT address.")
    index = int(address) - int(pool.network_address)
    if network.tap_device != f"tap{index}":
        raise ValueError("The sandbox network interface does not match its address lease.")
    uid = port = None
    resolvers: tuple[str, ...] = ()
    if policy.allowed_domains:
        uid, port = 100000 + index, 61000 + index
        if port > 65535:
            raise ValueError("This sandbox address is outside the strict proxy port pool.")
        first, last = map(int, Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split())
        if first <= port <= last:
            raise RuntimeError("The sandbox proxy port overlaps the host's temporary port range.")
        # NSS identities are never borrowed. Live process ownership is checked
        # under the placement lock, not here, so adoption remains possible.
        for lookup in (pwd.getpwuid, grp.getgrgid):
            try:
                lookup(uid)
            except KeyError:
                continue
            raise RuntimeError("The sandbox proxy identity is assigned to a host account.")
        found = set()
        for line in Path("/etc/resolv.conf").read_text().splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                resolver = ipaddress.ip_address(fields[1])
                if resolver.version == 4:
                    found.add(str(resolver))
        if not found:
            raise RuntimeError("Strict networking requires an IPv4 host DNS resolver.")
        resolvers = tuple(sorted(found))
    inventory = subprocess.run(
        ["ip", "-j", "-4", "address", "show"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    addresses = {
        str(ipaddress.IPv4Address(item["local"]))
        for interface in json.loads(inventory.stdout)
        for item in interface.get("addr_info", [])
        if item.get("family") == "inet"
    }
    if network.gateway_ip not in addresses:
        raise RuntimeError("The sandbox gateway address is missing from this host.")
    binding = NetworkBinding(
        tap=network.tap_device,
        guest_ip=network.guest_ip,
        gateway_ip=network.gateway_ip,
        host_addresses=tuple(sorted(addresses)),
        resolver_addresses=resolvers,
        proxy_uid=uid,
        proxy_port=port,
    )
    return binding, STATE_DIRECTORY / f"{network.tap_device}.json"


def saved_placement(vm: VMInfo) -> tuple[NetworkBinding, Path]:
    """Read host-owned placement without rediscovering or repairing live rules."""

    try:
        network = vm.network
        policy = vm.config.network_policy
        if network is None or network.mode != "nat" or policy is None:
            raise ValueError("Missing strict NAT configuration.")
        address = ipaddress.IPv4Address(network.guest_ip)
        pool = ipaddress.IPv4Network("172.16.0.0/16")
        index = int(address) - int(pool.network_address)
        if address not in pool or network.tap_device != f"tap{index}":
            raise ValueError("Invalid placement.")
        path = STATE_DIRECTORY / f"tap{index}.json"
        saved = json.loads(path.read_text())
        binding = NetworkBinding(**saved["binding"])
        uid, port = (100000 + index, 61000 + index) if policy.allowed_domains else (None, None)
        if (
            binding.tap != network.tap_device
            or binding.guest_ip != network.guest_ip
            or binding.gateway_ip != network.gateway_ip
            or binding.proxy_uid != uid
            or binding.proxy_port != port
            or binding.platform_ports
        ):
            raise ValueError("Placement changed.")
        return binding, path
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError(
            f"Sandbox '{vm.vm_id}' network protection could not be verified; "
            f"stop it with 'smolvm sandbox stop {vm.vm_id}'."
        ) from error


def active_policy(vm: VMInfo, vm_pid: int) -> tuple[NetworkBinding, dict]:
    """Adopt only a live supervisor protecting the same VM process."""
    from .lifecycle import policy_status

    binding, path = saved_placement(vm)
    state = policy_status(path, vm.config.network_policy, binding, vm_pid)
    if state is None:
        raise RuntimeError(
            f"Sandbox '{vm.vm_id}' network protection could not be verified; "
            f"stop it with 'smolvm sandbox stop {vm.vm_id}'."
        )
    return binding, state
