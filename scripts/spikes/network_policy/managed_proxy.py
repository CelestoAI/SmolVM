"""Import the runtime worker owner in the isolated test harness."""

import sys
from pathlib import Path

if not (Path(__file__).parent / "network_policy").is_dir():
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src/smolvm"))
from network_policy.process import ManagedProxy  # noqa: E402, F401
