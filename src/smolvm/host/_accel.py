"""Native acceleration dispatcher.

Tries to import smolvm-core for fast network operations.
Falls back gracefully if not installed or not available on this platform.
"""

try:
    from smolvm_core import (
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
