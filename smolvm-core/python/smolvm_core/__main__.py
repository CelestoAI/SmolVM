"""Print smolvm-core capabilities as JSON."""

from __future__ import annotations

import json
import platform

from . import __version__, detect


def main() -> None:
    """Print a zero-side-effect capability report."""

    report = {
        "smolvm_core": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "capabilities": detect().as_dict(),
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
