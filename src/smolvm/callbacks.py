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

"""Class-based callbacks for hooking into the SmolVM command lifecycle.

A callback is a plain object that subclasses :class:`Callback` and overrides
only the ``on_*`` hooks it cares about; every hook defaults to a no-op, so a
callback never has to implement methods it does not use (the same shape as
Keras and PyTorch Lightning callbacks).

Callbacks are attached per-VM and fire around :meth:`smolvm.SmolVM.run`::

    from smolvm import SmolVM, Callback, CommandBlockedError

    class SafetyGuard(Callback):
        DENY = ("rm -rf /", "mkfs", ":(){ :|:& };:")

        def on_pre_run(self, ctx):
            if any(bad in ctx.command for bad in self.DENY):
                raise CommandBlockedError(
                    f"Blocked unsafe command: {ctx.command!r}"
                )

    with SmolVM(config, callbacks=[SafetyGuard()]) as vm:
        vm.run("rm -rf /")   # raises CommandBlockedError; never reaches guest

Hook contract:

* ``on_pre_run`` is the **veto** channel. If it raises, the command is aborted
  before it ever reaches the guest and the exception propagates to the caller.
  Raise :class:`CommandBlockedError` for an explicit, typed block.
* ``on_post_run`` and ``on_run_error`` are passive **observers**. Exceptions
  they raise are caught and logged so a faulty observer can never break a
  command that already ran (or already failed); they do not propagate.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from smolvm.exceptions import SmolVMError
from smolvm.types import CommandResult

logger = logging.getLogger(__name__)


class CommandBlockedError(SmolVMError):
    """Raised to veto a command from within :meth:`Callback.on_pre_run`.

    Carries the offending command and VM id in ``details`` for logging.
    """

    def __init__(self, message: str, vm_id: str | None = None, command: str | None = None) -> None:
        super().__init__(message, {"vm_id": vm_id, "command": command})
        self.vm_id = vm_id
        self.command = command


@dataclass
class RunContext:
    """State passed to every command hook for a single :meth:`SmolVM.run` call.

    Using one object (rather than loose keyword arguments) means new fields can
    be added later without changing any callback's method signature.

    Attributes:
        vm_id: The VM the command targets.
        command: The shell command as passed to ``run()``.
        shell: Execution mode — ``"login"`` or ``"raw"``.
        timeout: Per-command timeout in seconds.
        result: The command result. ``None`` until ``on_post_run``.
        error: The transport error raised during execution. ``None`` unless
            the hook is ``on_run_error``.
    """

    vm_id: str
    command: str
    shell: str
    timeout: int
    result: CommandResult | None = None
    error: Exception | None = None


class Callback:
    """Base class for SmolVM command-lifecycle callbacks.

    Subclass this and override only the hooks you need; the defaults are
    no-ops. See the module docstring for the veto-vs-observer contract.
    """

    def on_pre_run(self, ctx: RunContext) -> None:
        """Called before a command is sent to the guest.

        Raise to abort the command (e.g. :class:`CommandBlockedError`). The
        exception propagates to the ``run()`` caller and nothing is executed.
        """

    def on_post_run(self, ctx: RunContext) -> None:
        """Called after a command completes. ``ctx.result`` is populated.

        Observer hook — exceptions are logged and swallowed.
        """

    def on_run_error(self, ctx: RunContext) -> None:
        """Called when the transport raised while executing a command.

        ``ctx.error`` is populated. Observer hook — exceptions are logged and
        swallowed, and the original transport error still propagates.
        """


class CallbackDispatcher:
    """Fans a single lifecycle event out to every registered callback in order.

    Internal helper used by :class:`~smolvm.facade.SmolVM`. ``on_pre_run`` is
    dispatched with ``propagate=True`` so a veto reaches the caller; observer
    hooks are dispatched with ``propagate=False`` so one bad callback cannot
    break the run.
    """

    def __init__(self, callbacks: Iterable[Callback] | None = None) -> None:
        self._callbacks: list[Callback] = list(callbacks or [])

    def add(self, callback: Callback) -> None:
        if not isinstance(callback, Callback):
            raise TypeError(f"callback must be a Callback instance, got {type(callback).__name__}")
        self._callbacks.append(callback)

    def __len__(self) -> int:
        return len(self._callbacks)

    def fire(self, hook: str, ctx: RunContext, *, propagate: bool) -> None:
        """Invoke ``hook`` on every callback.

        Args:
            hook: Name of the ``on_*`` method to call.
            ctx: The shared run context.
            propagate: If True, a callback's exception is re-raised to the
                caller (veto semantics). If False, it is logged and swallowed
                (observer semantics).
        """
        for callback in self._callbacks:
            method = getattr(callback, hook)
            try:
                method(ctx)
            except Exception:
                if propagate:
                    raise
                logger.exception(
                    "Callback %s.%s raised; ignoring (vm_id=%s, command=%r)",
                    type(callback).__name__,
                    hook,
                    ctx.vm_id,
                    ctx.command,
                )
