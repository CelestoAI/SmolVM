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

# Tests configuration

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

_SPARSE_TEST_HOLE_BYTES = 32 * 1024 * 1024
_SPARSE_TEST_FALLBACK_BYTES = 1 * 1024 * 1024


@pytest.fixture(scope="session")
def sparse_test_config(tmp_path_factory: pytest.TempPathFactory) -> tuple[int, bool]:
    """Adapt sparse-file assertions and fixture sizes to the test volume.

    Some macOS volumes report small files with holes as fully allocated, while
    volumes without sparse-file support cannot satisfy allocation assertions at
    any size. Probe the same write-and-seek pattern as the copy fallback. Tests
    keep their content checks but use smaller fixtures when holes are unavailable.
    """
    probe = tmp_path_factory.mktemp("sparse-probe") / "probe.img"
    with probe.open("wb") as file:
        first_chunk = b"start" + b"\0" * (_SPARSE_TEST_FALLBACK_BYTES - len(b"start"))
        file.write(first_chunk)
        file.seek(_SPARSE_TEST_HOLE_BYTES - len(first_chunk), os.SEEK_CUR)
        file.write(b"end")

    stat = probe.stat()
    blocks = getattr(stat, "st_blocks", None)
    supports_sparse_allocation = blocks is not None and blocks * 512 < stat.st_size
    hole_bytes = (
        _SPARSE_TEST_HOLE_BYTES if supports_sparse_allocation else _SPARSE_TEST_FALLBACK_BYTES
    )
    return hole_bytes, supports_sparse_allocation


@pytest.fixture(autouse=True, scope="session")
def _plain_cli_output() -> Iterator[None]:
    """Pin Rich's rendering so CLI assertions do not depend on the shell.

    Many CLI tests assert on exact substrings of rendered output. Rich honours
    FORCE_COLOR and TERM from the environment, so a developer (or CI runner)
    who exports FORCE_COLOR=1 gets ANSI escapes interleaved into that output
    and those assertions fail for a reason that has nothing to do with the code
    under test. Neutralise the colour signals for the whole session; the
    product still honours them for real users.
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    try:
        yield
    finally:
        monkeypatch.undo()
