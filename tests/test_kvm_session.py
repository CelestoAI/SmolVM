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

"""Tests for the kvm-group session re-exec helper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from smolvm.cli import _kvm_session


@pytest.fixture(autouse=True)
def _reset_reexec_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip both kvm-reexec env vars before every test.

    Several tests in this module exercise the real ``_should_attempt_reexec``
    flow and would short-circuit if a previous test left
    ``SMOLVM_KVM_REEXEC_DONE`` or ``SMOLVM_NO_KVM_REEXEC`` set in this
    process — notably the exec-path test below, which mocks ``os.execvp`` and
    so doesn't actually replace the process the way real execvp would.
    """
    monkeypatch.delenv("SMOLVM_KVM_REEXEC_DONE", raising=False)
    monkeypatch.delenv("SMOLVM_NO_KVM_REEXEC", raising=False)


def _mock_kvm_grp(members: list[str], gid: int = 999) -> SimpleNamespace:
    return SimpleNamespace(gr_mem=members, gr_gid=gid)


def _mock_pwd_user(name: str) -> SimpleNamespace:
    return SimpleNamespace(pw_name=name)


class TestShouldAttemptReexec:
    """Cover every branch of the gate function."""

    @patch("smolvm.cli._kvm_session.platform.system", return_value="Darwin")
    def test_macos_skips(self, _mock_system: MagicMock) -> None:
        # macOS users must never see this code path attempt anything — no
        # /dev/kvm, no `sg`, no `kvm` group. Returning False here is the
        # whole reason we can ship the import in cross-platform main.py.
        assert _kvm_session._should_attempt_reexec([]) is False

    @patch.dict(
        "smolvm.cli._kvm_session.os.environ",
        {"SMOLVM_KVM_REEXEC_DONE": "1"},
        clear=False,
    )
    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_loop_guard_env_skips(self, _mock_system: MagicMock) -> None:
        assert _kvm_session._should_attempt_reexec([]) is False

    @patch.dict(
        "smolvm.cli._kvm_session.os.environ",
        {"SMOLVM_NO_KVM_REEXEC": "1"},
        clear=False,
    )
    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_user_disable_env_skips(self, _mock_system: MagicMock) -> None:
        assert _kvm_session._should_attempt_reexec([]) is False

    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_doctor_subcommand_skips(self, _mock_system: MagicMock) -> None:
        # Doctor is diagnostic — silently re-execing would mask the very
        # state the user invoked it to inspect.
        assert _kvm_session._should_attempt_reexec(["doctor"]) is False

    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_setup_subcommand_skips(self, _mock_system: MagicMock) -> None:
        assert _kvm_session._should_attempt_reexec(["setup"]) is False

    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_help_short_circuits(self, _mock_system: MagicMock) -> None:
        assert _kvm_session._should_attempt_reexec(["--help"]) is False

    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_verb_level_help_short_circuits(self, _mock_system: MagicMock) -> None:
        # ``smolvm sandbox create --help`` reaches ["sandbox", "create", "--help"] —
        # this should not trigger a re-exec even though ``create`` is a
        # kvm-using verb, because no kvm work will actually run.
        assert _kvm_session._should_attempt_reexec(["sandbox", "create", "--help"]) is False
        assert _kvm_session._should_attempt_reexec(["sandbox", "snapshot", "create", "-h"]) is False
        assert (
            _kvm_session._should_attempt_reexec(["sandbox", "create", "--name", "x", "-V"]) is False
        )

    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_read_only_verbs_skip(self, _mock_system: MagicMock) -> None:
        # Read-only / VM-process-targeted verbs don't need /dev/kvm; a
        # re-exec for them would just print a confusing notice.
        skipped = [
            ["sandbox", "list"],
            ["sandbox", "info"],
            ["sandbox", "ssh"],
            ["sandbox", "stop"],
            ["sandbox", "pause"],
            ["sandbox", "delete"],
            ["sandbox", "env"],
            ["sandbox", "file"],
            ["sandbox", "snapshot", "list"],
            ["sandbox", "port"],
        ]
        for argv in skipped:
            assert _kvm_session._should_attempt_reexec(argv) is False, (
                f"expected {argv!r} to skip re-exec"
            )

    @patch("smolvm.cli._kvm_session._KVM_DEV")
    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_no_dev_kvm_skips(self, _mock_system: MagicMock, mock_dev: MagicMock) -> None:
        mock_dev.exists.return_value = False
        assert _kvm_session._should_attempt_reexec(["sandbox", "create"]) is False

    @patch("smolvm.cli._kvm_session.os.access", return_value=True)
    @patch("smolvm.cli._kvm_session._KVM_DEV")
    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_already_accessible_skips(
        self,
        _mock_system: MagicMock,
        mock_dev: MagicMock,
        _mock_access: MagicMock,
    ) -> None:
        mock_dev.exists.return_value = True
        assert _kvm_session._should_attempt_reexec(["sandbox", "create"]) is False

    @patch("smolvm.cli._kvm_session.os.getuid", return_value=1000)
    @patch("smolvm.cli._kvm_session.os.access", return_value=False)
    @patch("smolvm.cli._kvm_session._KVM_DEV")
    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_user_not_in_kvm_group_skips(
        self,
        _mock_system: MagicMock,
        mock_dev: MagicMock,
        _mock_access: MagicMock,
        _mock_uid: MagicMock,
    ) -> None:
        # Re-execing under `sg kvm` for a user who is *not* a kvm member
        # would prompt for the group password — never acceptable mid-CLI.
        mock_dev.exists.return_value = True
        with (
            patch("grp.getgrnam", return_value=_mock_kvm_grp(members=["other"])),
            patch("pwd.getpwuid", return_value=_mock_pwd_user("alice")),
        ):
            assert _kvm_session._should_attempt_reexec(["sandbox", "create"]) is False

    @patch("smolvm.cli._kvm_session.os.getgroups", return_value=[999])
    @patch("smolvm.cli._kvm_session.os.getuid", return_value=1000)
    @patch("smolvm.cli._kvm_session.os.access", return_value=False)
    @patch("smolvm.cli._kvm_session._KVM_DEV")
    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_kvm_already_in_effective_groups_skips(
        self,
        _mock_system: MagicMock,
        mock_dev: MagicMock,
        _mock_access: MagicMock,
        _mock_uid: MagicMock,
        _mock_groups: MagicMock,
    ) -> None:
        # The group is already in our effective set but /dev/kvm is still
        # denied — that's a different problem (mode bits, ACLs); re-exec
        # would loop or hide it. Doctor will surface the real reason.
        mock_dev.exists.return_value = True
        with (
            patch(
                "grp.getgrnam",
                return_value=_mock_kvm_grp(members=["alice"], gid=999),
            ),
            patch("pwd.getpwuid", return_value=_mock_pwd_user("alice")),
        ):
            assert _kvm_session._should_attempt_reexec(["sandbox", "create"]) is False

    @patch("smolvm.cli._kvm_session.os.getgroups", return_value=[1000])
    @patch("smolvm.cli._kvm_session.os.getuid", return_value=1000)
    @patch("smolvm.cli._kvm_session.os.access", return_value=False)
    @patch("smolvm.cli._kvm_session._KVM_DEV")
    @patch("smolvm.cli._kvm_session.platform.system", return_value="Linux")
    def test_pending_kvm_membership_qualifies(
        self,
        _mock_system: MagicMock,
        mock_dev: MagicMock,
        _mock_access: MagicMock,
        _mock_uid: MagicMock,
        _mock_groups: MagicMock,
    ) -> None:
        mock_dev.exists.return_value = True
        with (
            patch(
                "grp.getgrnam",
                return_value=_mock_kvm_grp(members=["alice"], gid=999),
            ),
            patch("pwd.getpwuid", return_value=_mock_pwd_user("alice")),
        ):
            assert _kvm_session._should_attempt_reexec(["sandbox", "create"]) is True


class TestMaybeReexec:
    """Cover the outer driver: skip path, missing-sg path, and exec path."""

    @patch("smolvm.cli._kvm_session._should_attempt_reexec", return_value=False)
    @patch("smolvm.cli._kvm_session.os.execvp")
    def test_skip_path_does_not_exec(
        self,
        mock_execvp: MagicMock,
        _mock_should: MagicMock,
    ) -> None:
        _kvm_session.maybe_reexec_for_kvm_group([])
        mock_execvp.assert_not_called()

    @patch("smolvm.cli._kvm_session.shutil.which", return_value=None)
    @patch("smolvm.cli._kvm_session._should_attempt_reexec", return_value=True)
    @patch("smolvm.cli._kvm_session.os.execvp")
    def test_missing_sg_returns_silently(
        self,
        mock_execvp: MagicMock,
        _mock_should: MagicMock,
        _mock_which: MagicMock,
    ) -> None:
        # If `sg` is unavailable we surface nothing and let the regular
        # kvm-permission failure carry the message.
        _kvm_session.maybe_reexec_for_kvm_group([])
        mock_execvp.assert_not_called()

    @patch("smolvm.cli._kvm_session.shutil.which", return_value="/usr/bin/sg")
    @patch("smolvm.cli._kvm_session._should_attempt_reexec", return_value=True)
    @patch("smolvm.cli._kvm_session.os.execvp")
    def test_exec_path_invokes_sg_kvm(
        self,
        mock_execvp: MagicMock,
        _mock_should: MagicMock,
        _mock_which: MagicMock,
    ) -> None:
        _kvm_session.maybe_reexec_for_kvm_group([])

        mock_execvp.assert_called_once()
        prog, argv = mock_execvp.call_args.args
        assert prog == "/usr/bin/sg"
        assert argv[:3] == ["/usr/bin/sg", "kvm", "-c"]
        # The inner command must invoke the same Python interpreter so
        # that the re-exec preserves the user's installed smolvm.
        import sys

        assert sys.executable in argv[3]

    @patch(
        "smolvm.cli._kvm_session.os.execvp",
        side_effect=OSError("sg vanished between which() and execvp()"),
    )
    @patch("smolvm.cli._kvm_session.shutil.which", return_value="/usr/bin/sg")
    @patch("smolvm.cli._kvm_session._should_attempt_reexec", return_value=True)
    def test_exec_failure_clears_loop_guard_and_reraises(
        self,
        _mock_should: MagicMock,
        _mock_which: MagicMock,
        _mock_execvp: MagicMock,
    ) -> None:
        # If execvp raises, the loop-guard env var must not linger in this
        # process — otherwise subsequent invocations within the same parent
        # would silently skip the re-exec on a stale marker. The original
        # exception must still propagate so the user sees the real failure.
        import os

        with pytest.raises(OSError, match="vanished"):
            _kvm_session.maybe_reexec_for_kvm_group([])
        assert os.environ.get("SMOLVM_KVM_REEXEC_DONE") is None
