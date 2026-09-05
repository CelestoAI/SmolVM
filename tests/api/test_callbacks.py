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

"""Tests for the command-lifecycle callback system."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from smolvm import Callback, CommandBlockedError, RunContext, SmolVM
from smolvm.callbacks import CallbackDispatcher
from smolvm.types import CommandResult, VMState


def _make_vm(
    callbacks: list[Callback] | None = None,
    run_result: CommandResult | None = None,
) -> SmolVM:
    """Build a SmolVM wired just enough to exercise run()'s callback path.

    Bypasses __init__ (which would create a real manager / VM) and stubs the
    gating attributes run() checks before delegating to the SSH client.
    """
    vm = SmolVM.__new__(SmolVM)
    vm._vm_id = "vm-test"
    vm._refresh_info = MagicMock()  # type: ignore[method-assign]
    vm.can_run_commands = MagicMock(return_value=True)  # type: ignore[method-assign]
    vm._info = MagicMock(status=VMState.RUNNING, network=MagicMock())
    vm._ssh_ready = True
    result = run_result or CommandResult(exit_code=0, stdout="ok", stderr="")
    vm._ssh = MagicMock()
    vm._ssh.run.return_value = result
    vm._callbacks = CallbackDispatcher(callbacks)
    return vm


def test_pre_run_veto_blocks_command() -> None:
    class Guard(Callback):
        def on_pre_run(self, ctx: RunContext) -> None:
            if "rm -rf /" in ctx.command:
                raise CommandBlockedError("unsafe", vm_id=ctx.vm_id, command=ctx.command)

    vm = _make_vm(callbacks=[Guard()])

    with pytest.raises(CommandBlockedError) as excinfo:
        vm.run("rm -rf /")

    assert excinfo.value.command == "rm -rf /"
    vm._ssh.run.assert_not_called()  # command never reached the guest


def test_allowed_command_runs_and_fires_post_run() -> None:
    seen: list[RunContext] = []

    class Recorder(Callback):
        def on_post_run(self, ctx: RunContext) -> None:
            seen.append(ctx)

    vm = _make_vm(callbacks=[Recorder()])
    result = vm.run("echo hi")

    assert result.stdout == "ok"
    vm._ssh.run.assert_called_once()
    assert len(seen) == 1
    assert seen[0].command == "echo hi"
    assert seen[0].result is result


def test_pre_run_receives_full_context() -> None:
    captured: list[tuple] = []

    class Recorder(Callback):
        def on_pre_run(self, ctx: RunContext) -> None:
            # Snapshot inside the hook: the ctx object is reused across the
            # run, so result is only None *at this point*, before execution.
            captured.append((ctx.vm_id, ctx.command, ctx.shell, ctx.timeout, ctx.result))

    vm = _make_vm(callbacks=[Recorder()])
    vm.run("uname -r", timeout=12, shell="raw")

    assert captured[0] == ("vm-test", "uname -r", "raw", 12, None)


def test_callbacks_fire_in_registration_order() -> None:
    order: list[str] = []

    class A(Callback):
        def on_pre_run(self, ctx: RunContext) -> None:
            order.append("a")

    class B(Callback):
        def on_pre_run(self, ctx: RunContext) -> None:
            order.append("b")

    vm = _make_vm(callbacks=[A(), B()])
    vm.run("true")
    assert order == ["a", "b"]


def test_observer_exception_is_swallowed() -> None:
    class Faulty(Callback):
        def on_post_run(self, ctx: RunContext) -> None:
            raise RuntimeError("boom")

    vm = _make_vm(callbacks=[Faulty()])
    # A broken observer must not break a command that already ran.
    result = vm.run("echo hi")
    assert result.stdout == "ok"


def test_run_error_hook_fires_and_original_error_propagates() -> None:
    seen: list[RunContext] = []

    class Watcher(Callback):
        def on_run_error(self, ctx: RunContext) -> None:
            seen.append(ctx)

    vm = _make_vm(callbacks=[Watcher()])
    boom = RuntimeError("transport down")
    vm._ssh.run.side_effect = boom

    with pytest.raises(RuntimeError, match="transport down"):
        vm.run("echo hi")

    assert len(seen) == 1
    assert seen[0].error is boom


def test_add_callback_registers_and_chains() -> None:
    class Guard(Callback):
        def on_pre_run(self, ctx: RunContext) -> None:
            raise CommandBlockedError("nope")

    vm = _make_vm()
    returned = vm.add_callback(Guard())
    assert returned is vm  # chainable

    with pytest.raises(CommandBlockedError):
        vm.run("anything")


def test_add_callback_rejects_non_callback() -> None:
    vm = _make_vm()
    with pytest.raises(TypeError):
        vm.add_callback(object())  # type: ignore[arg-type]


def test_no_callbacks_is_noop() -> None:
    vm = _make_vm()
    result = vm.run("echo hi")
    assert result.stdout == "ok"
    vm._ssh.run.assert_called_once()
