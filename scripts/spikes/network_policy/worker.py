"""Compatibility entry point for testing the runtime's isolated worker."""

import runpy
import sys
from pathlib import Path

worker = Path(__file__).resolve().parents[3] / "src/smolvm/network_policy/worker.py"
sys.path.insert(0, str(worker.parent))
runpy.run_path(str(worker), run_name="__main__")
