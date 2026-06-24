"""Linux networking helpers for TAP devices, routes, and sysctls."""

from . import _ffi


def available() -> bool:
    """Return True when this wheel includes Linux networking helpers."""

    return bool(_ffi.has_native_networking())


def create_tap(name: str, owner_uid: int) -> None:
    """Create a TAP network device owned by ``owner_uid``."""

    _ffi.create_tap(name, owner_uid)


def delete_tap(name: str) -> None:
    """Delete a TAP network device."""

    _ffi.delete_tap(name)


def set_link_up(name: str) -> None:
    """Bring a network link up."""

    _ffi.set_link_up(name)


def flush_addrs(name: str) -> None:
    """Remove all addresses from a network link."""

    _ffi.flush_addrs(name)


def add_addr(name: str, ip: str, prefix_len: int) -> None:
    """Add an IPv4 address with prefix length to a network link."""

    _ffi.add_addr(name, ip, prefix_len)


def configure_tap(name: str, host_ip: str, prefix_len: int) -> None:
    """Assign an IPv4 address to a TAP link and bring it up."""

    _ffi.configure_tap(name, host_ip, prefix_len)


def prepare_tap(
    name: str,
    owner_uid: int,
    host_ip: str,
    prefix_len: int,
    route_localnet: bool = True,
) -> None:
    """Create and configure a TAP link in one native operation."""

    _ffi.prepare_tap(name, owner_uid, host_ip, prefix_len, route_localnet)


def add_route(dest: str, prefix_len: int, dev: str) -> None:
    """Add a route for ``dest/prefix_len`` through ``dev``."""

    _ffi.add_route(dest, prefix_len, dev)


def get_default_interface() -> str:
    """Return the default outbound network interface name."""

    return str(_ffi.get_default_interface())


def write_sysctl(key: str, value: str) -> None:
    """Write a Linux sysctl key using dot notation."""

    _ffi.write_sysctl(key, value)


__all__ = [
    "add_addr",
    "add_route",
    "available",
    "configure_tap",
    "create_tap",
    "delete_tap",
    "flush_addrs",
    "get_default_interface",
    "prepare_tap",
    "set_link_up",
    "write_sysctl",
]
