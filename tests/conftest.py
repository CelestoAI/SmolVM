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

from collections.abc import Iterator

import pytest


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
