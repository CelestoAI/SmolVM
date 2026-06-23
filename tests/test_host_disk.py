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

"""Tests for host disk helper fallback boundaries."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.host import disk


def test_clone_or_sparse_copy_prefers_cp_before_native(tmp_path: Path) -> None:
    """GNU cp should stay the production default for raw disk clones."""
    source = tmp_path / "source.ext4"
    target = tmp_path / "target.ext4"
    native = MagicMock()

    with (
        patch.object(disk, "_cp_clone_or_sparse_copy", return_value="cp") as mock_cp,
        patch.object(disk, "_native_available", side_effect=AssertionError("native skipped")),
        patch.object(disk, "_native", native),
    ):
        assert disk.clone_or_sparse_copy(source, target) == "cp"

    mock_cp.assert_called_once_with(source, target)
    native.clone_or_sparse_copy.assert_not_called()


def test_clone_or_sparse_copy_uses_native_after_cp_failure(tmp_path: Path) -> None:
    """Native copy remains available after cp cannot handle the clone."""
    source = tmp_path / "source.ext4"
    target = tmp_path / "target.ext4"
    native = MagicMock()
    native.clone_or_sparse_copy.return_value = "sparse"

    with (
        patch.object(disk, "_cp_clone_or_sparse_copy", return_value=None) as mock_cp,
        patch.object(disk, "_native_available", return_value=True),
        patch.object(disk, "_native", native),
        patch.object(disk.shutil, "copystat") as mock_copystat,
        patch.object(
            disk,
            "_python_clone_or_sparse_copy",
            side_effect=AssertionError("must not fall back"),
        ),
    ):
        assert disk.clone_or_sparse_copy(source, target) == "native:sparse"

    mock_cp.assert_called_once_with(source, target)
    native.clone_or_sparse_copy.assert_called_once_with(str(source), str(target))
    mock_copystat.assert_called_once_with(source, target)


def test_clone_or_sparse_copy_uses_python_when_cp_and_native_unavailable(tmp_path: Path) -> None:
    """Disk copies should keep the Python sparse fallback path."""
    source = tmp_path / "source.ext4"
    target = tmp_path / "target.ext4"

    with (
        patch.object(disk, "_cp_clone_or_sparse_copy", return_value=None) as mock_cp,
        patch.object(disk, "_native_available", return_value=False),
        patch.object(disk, "_python_clone_or_sparse_copy", return_value="sparse") as mock_python,
    ):
        assert disk.clone_or_sparse_copy(source, target) == "sparse"

    mock_cp.assert_called_once_with(source, target)
    mock_python.assert_called_once_with(source, target)


def test_clone_or_sparse_copy_propagates_native_oserror(tmp_path: Path) -> None:
    """Native copy failures after invocation must not silently recopy in Python."""
    native = MagicMock()
    native.clone_or_sparse_copy.side_effect = OSError("No space left on device")

    with (
        patch.object(disk, "_cp_clone_or_sparse_copy", return_value=None),
        patch.object(disk, "_native_available", return_value=True),
        patch.object(disk, "_native", native),
        patch.object(
            disk,
            "_python_clone_or_sparse_copy",
            side_effect=AssertionError("must not fall back"),
        ),
        pytest.raises(OSError, match="No space left on device"),
    ):
        disk.clone_or_sparse_copy(tmp_path / "source.ext4", tmp_path / "target.ext4")


def test_decompress_zstd_sparse_propagates_native_oserror(tmp_path: Path) -> None:
    """Native decompression failures after invocation must remain visible."""
    native = MagicMock()
    native.decompress_zstd_sparse.side_effect = OSError("corrupt zstd stream")

    with (
        patch.object(disk, "_native_available", return_value=True),
        patch.object(disk, "_native", native),
        patch.object(
            disk,
            "_python_decompress_zstd_sparse",
            side_effect=AssertionError("must not fall back"),
        ),
        pytest.raises(OSError, match="corrupt zstd stream"),
    ):
        disk.decompress_zstd_sparse(tmp_path / "rootfs.ext4.zst", tmp_path / "rootfs.ext4")
