"""Private process entry point; the supervisor outlives an SDK/CLI request."""

import logging
import sys
from pathlib import Path

logging.disable(sys.maxsize)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from network_policy.lifecycle import supervise  # noqa: E402

if __name__ == "__main__":
    try:
        supervise()
    except BaseException:
        # Never emit payloads, environment, certificates or exception tracebacks.
        raise SystemExit(2) from None
