"""Native network acceleration dispatcher."""

import logging
import sys

logger = logging.getLogger(__name__)

try:
    from smolvm_core import network as network_native

    HAS_NETLINK = network_native.available()
except ImportError:
    network_native = None  # type: ignore[assignment]
    HAS_NETLINK = False

if not HAS_NETLINK and sys.platform == "linux":
    logger.warning(
        "smolvm-core native extension is unavailable; falling back to subprocess "
        "(ip/nft/sysctl) for network operations, which is significantly slower. "
        "Reinstall smolvm to pick up the native wheel."
    )

__all__ = ["HAS_NETLINK", "network_native"]
