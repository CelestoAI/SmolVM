"""Verified native shutdown before releasing host policy resources."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smolvm.types import VMInfo

from .lifecycle import cleanup_stopped_policy, process_identity
from .placement import saved_placement


def stop_policy(vm: VMInfo, process: subprocess.Popen | None, *, timeout: float) -> None:
    """Stop by birth identity, retaining all placement evidence on uncertainty."""
    binding, path = saved_placement(vm)
    state = json.loads(path.read_text())
    pid, identity = state.get("vm_pid"), state.get("vm_identity")
    if identity:
        if vm.pid is not None and vm.pid != pid:
            raise RuntimeError(
                f"Sandbox '{vm.vm_id}' process ownership changed; retain its reservation."
            )
        try:
            handle = os.pidfd_open(pid)
        except ProcessLookupError:
            handle = None
        if handle is not None:
            try:
                # An old PID can now belong to somebody else. Never signal it.
                if process_identity(pid) == identity:
                    # Exit can race either signal. Still require the kernel
                    # handle to become readable before releasing resources.
                    with suppress(ProcessLookupError):
                        signal.pidfd_send_signal(handle, signal.SIGTERM)
                    if not select.select([handle], [], [], timeout)[0]:
                        with suppress(ProcessLookupError):
                            signal.pidfd_send_signal(handle, signal.SIGKILL)
                        if not select.select([handle], [], [], 5)[0]:
                            raise RuntimeError(
                                f"Sandbox '{vm.vm_id}' did not stop; retain its reservation."
                            )
            finally:
                os.close(handle)
        if process is not None:
            process.wait(timeout=5)
    elif process is not None:
        # The launching SDK owns this child even if attachment never completed.
        process.kill()
        process.wait(timeout=timeout)
    else:
        # A reserved/starting record cannot prove an interrupted launch never
        # spawned QEMU. Do not release a possibly occupied address.
        raise RuntimeError(
            f"Sandbox '{vm.vm_id}' shutdown could not be verified; retain its reservation "
            "and ask the sandbox service operator to check the stopped process."
        )
    # The supervisor observes VM death and fences/closes its worker. Wait for
    # its lock rather than releasing the IP while that teardown is in flight.
    deadline = time.monotonic() + max(timeout, 5)
    while True:
        try:
            cleanup_stopped_policy(path, binding)
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)
