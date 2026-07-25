# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared helpers for QEMU incremental snapshot tests."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _run(*command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, capture_output=True, text=True)


def _materialize(qemu_img: str, root: Path, name: str, artifacts: list[Path]) -> Path:
    """Build and validate a standalone qcow2 from an ordered artifact chain."""
    if not artifacts:
        raise ValueError("artifacts cannot be empty")
    expected_virtual_size = json.loads(
        _run(qemu_img, "info", "--output=json", str(artifacts[0])).stdout
    )["virtual-size"]
    staging = root / f"materialize-{name}"
    staging.mkdir()
    local_artifacts: list[Path] = []
    for depth, artifact in enumerate(artifacts):
        local = staging / f"depth-{depth}.qcow2"
        shutil.copy2(artifact, local)
        local_artifacts.append(local)
        if depth > 0:
            _run(
                qemu_img,
                "rebase",
                "-u",
                "-f",
                "qcow2",
                "-F",
                "qcow2",
                "-b",
                str(local_artifacts[depth - 1]),
                str(local),
            )

    _run(qemu_img, "check", str(local_artifacts[-1]))
    output = root / f"materialized-{name}.qcow2"
    _run(
        qemu_img,
        "convert",
        "-f",
        "qcow2",
        "-O",
        "qcow2",
        str(local_artifacts[-1]),
        str(output),
    )
    info = json.loads(_run(qemu_img, "info", "--output=json", str(output)).stdout)
    assert info["format"] == "qcow2"
    assert info["virtual-size"] == expected_virtual_size
    assert not info.get("backing-filename")
    _run(qemu_img, "check", str(output))
    return output
