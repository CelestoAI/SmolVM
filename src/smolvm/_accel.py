"""Netlink acceleration dispatcher.

Tries to import the Rust PyO3 extension for fast network operations.
Falls back gracefully to subprocess-based operations if unavailable
(e.g., on macOS, or when built without Rust toolchain).
"""

try:
    from smolvm._native import (  # type: ignore[import-not-found]
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

    HAS_NETLINK = is_available()
except ImportError:
    HAS_NETLINK = False

__all__ = ["HAS_NETLINK"]
