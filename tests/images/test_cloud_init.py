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

"""Tests for cloud-init seed ISO helpers."""

from unittest.mock import MagicMock, patch

import pytest

from smolvm.images.cloud_init import build_seed_iso


def test_build_seed_iso_replaces_output_atomically(tmp_path) -> None:
    """A successful write should replace the target file with the completed temp file."""
    output_path = tmp_path / "seed.iso"
    output_path.write_bytes(b"stale")

    build_seed_iso(
        output_path,
        user_data="#cloud-config\nusers: []\n",
        meta_data="instance-id: test\n",
    )

    assert output_path.exists()
    assert output_path.stat().st_size > 0
    assert not list(tmp_path.glob("*.tmp"))


def test_build_seed_iso_closes_and_cleans_up_temp_file_on_failure(tmp_path) -> None:
    """A failed ISO write should still close the image and preserve the prior output."""
    output_path = tmp_path / "seed.iso"
    output_path.write_text("stable-output")

    fake_iso = MagicMock()
    fake_iso.write.side_effect = RuntimeError("write failed")

    with (
        patch("smolvm.images.cloud_init.PyCdlib", return_value=fake_iso),
        pytest.raises(RuntimeError, match="write failed"),
    ):
        build_seed_iso(
            output_path,
            user_data="#cloud-config\nusers: []\n",
            meta_data="instance-id: test\n",
        )

    fake_iso.close.assert_called_once()
    assert output_path.read_text() == "stable-output"
    assert not list(tmp_path.glob("*.tmp"))
