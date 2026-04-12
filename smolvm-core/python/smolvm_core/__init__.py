"""SmolVM Core — Rust-accelerated network operations.

This package provides fast TAP device, route, and sysctl operations
via direct kernel netlink API calls, replacing subprocess calls to
ip, nft, and sysctl.

On Linux, all operations use netlink (zero subprocess overhead).
On macOS, functions are available but raise OSError (use smolvm's
subprocess fallback instead).
"""

from smolvm_core._smolvm_core import (
    add_addr,
    add_route,
    create_tap,
    delete_tap,
    flush_addrs,
    get_default_interface,
    is_available,
    set_link_up,
    write_sysctl,
)

__all__ = [
    "is_available",
    "create_tap",
    "delete_tap",
    "set_link_up",
    "flush_addrs",
    "add_addr",
    "add_route",
    "get_default_interface",
    "write_sysctl",
]
