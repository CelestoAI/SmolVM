"""Compatibility import for the original wire harness; use the shipped engine."""

import sys
from pathlib import Path

# Load the same standalone module used by the isolated worker without importing
# the SDK and its unrelated dependencies into this deliberately minimal harness.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src/smolvm/network_policy"))
from engine import make_proxy  # noqa: E402, F401
