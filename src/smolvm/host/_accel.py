"""Native acceleration dispatcher.

Tries to import smolvm-core for fast network operations.
Falls back gracefully if not installed or not available on this platform.

Callers should gate use on ``HAS_NETLINK``; ``native`` is ``None`` otherwise.
"""

import logging
import sys

logger = logging.getLogger(__name__)

try:
    import smolvm_core as native

    HAS_NETLINK = native.is_available()
except ImportError:
    native = None  # type: ignore[assignment]
    HAS_NETLINK = False

if not HAS_NETLINK and sys.platform == "linux":
    logger.warning(
        "smolvm-core native extension is unavailable; falling back to subprocess "
        "(ip/nft/sysctl) for network operations, which is significantly slower. "
        "Reinstall smolvm to pick up the native wheel."
    )

__all__ = ["HAS_NETLINK", "native"]
