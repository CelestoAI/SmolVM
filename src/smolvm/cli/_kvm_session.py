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

"""Auto-activate a pending ``kvm`` group membership for the current CLI run.

When a user runs ``sudo usermod -aG kvm $USER``, the new group only takes
effect for *new* login sessions. Their current shell (and any child
processes, including ``smolvm``) keeps the old group set, so ``/dev/kvm``
remains inaccessible until they log out and back in. This module detects
that exact state and re-execs the CLI under ``sg kvm -c …`` so the kvm
group is active for the rest of the run, sparing first-time users a
manual ``newgrp`` or relog.

Linux-only. Returns immediately on macOS and other platforms.
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

# Internal env var used as a loop guard so the re-exec'd child does not
# re-enter the helper and fork forever.
_REEXEC_DONE_ENV = "SMOLVM_KVM_REEXEC_DONE"

# User-facing escape hatch: setting this disables the re-exec entirely,
# useful for advanced users, scripts, or debugging.
_REEXEC_DISABLE_ENV = "SMOLVM_NO_KVM_REEXEC"

# Subcommands that should *not* trigger a re-exec. Two groups:
#
#   1. Diagnostic/administrative verbs whose job is to *report* state —
#      silently switching the process's primary gid would hide the very
#      thing the user invoked them to inspect (``doctor``) or fix
#      (``setup``).
#
#   2. Read-only or VM-process-targeted verbs that don't open ``/dev/kvm``
#      themselves — listing, inspecting, ssh'ing into an already-running
#      sandbox, signalling stop/pause, removing state files. Re-execing
#      under ``sg kvm`` would only add startup latency and a confusing
#      "activating kvm group" notice for an operation that doesn't need
#      kvm at all.
#
# ``-h``/``--help``/``-V``/``--version`` are checked separately because they
# can appear at *any* depth (e.g. ``smolvm sandbox create --help``), not just as
# the first argument.
_SKIP_FIRST_ARGS: frozenset[str] = frozenset(
    {
        # Diagnostic / administrative
        "doctor",
        "setup",
        "update",
        "prune",
        "ui",
        "server",
    }
)

_SKIP_SANDBOX_ACTIONS: frozenset[str] = frozenset(
    {
        "list",
        "info",
        "ssh",
        "stop",
        "pause",
        "resume",
        "delete",
        "env",
        "file",
        "port",
    }
)

_SKIP_SANDBOX_NESTED_ACTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("snapshot", "list"),
    }
)

_SKIP_BROWSER_ACTIONS: frozenset[str] = frozenset({"list", "stop", "open", "logs"})

# Help/version flags can appear anywhere in argv (top-level *or* per-verb,
# e.g. ``smolvm sandbox create --help``). When any of these is present, Click is
# going to short-circuit before any kvm work happens, so we should too.
_HELP_VERSION_FLAGS: frozenset[str] = frozenset({"-h", "--help", "-V", "--version"})

_KVM_DEV = Path("/dev/kvm")
_KVM_GROUP = "kvm"


def maybe_reexec_for_kvm_group(argv: Sequence[str] | None = None) -> None:
    """Re-exec via ``sg kvm -c`` when a usermod is pending a session refresh.

    ``argv`` defaults to ``sys.argv[1:]`` and is used only to decide whether
    the requested subcommand is one we deliberately skip (``doctor``,
    ``setup``, ``--help``, ``--version``). On a successful re-exec this
    function does not return — execution transfers to the ``sg`` child
    process. On any failure or skipped path it returns ``None`` and the
    caller proceeds normally.
    """
    if not _should_attempt_reexec(argv):
        return

    sg_path = shutil.which("sg")
    if sg_path is None:
        # Without `sg` we have no way to acquire the group without a relog;
        # let the regular kvm-permission failure surface with its fix hint.
        return

    # Mark the env so the child doesn't loop, then exec. After execvp
    # succeeds, this Python process is replaced and never returns; if it
    # raises (rare — sg disappeared between which() and execvp(), bad
    # interpreter, etc.) we must drop the marker so the regular kvm
    # permission failure surfaces normally instead of being silenced by a
    # loop guard meant for the *next* process.
    child_env_marker_set()
    inner_cmd = shlex.join([sys.executable, *sys.argv])
    print(
        "Activating pending kvm group membership for this session "
        "(via 'sg kvm') so /dev/kvm becomes accessible…",
        file=sys.stderr,
        flush=True,
    )
    try:
        os.execvp(sg_path, [sg_path, _KVM_GROUP, "-c", inner_cmd])
    except BaseException:
        # execvp normally never returns; if it raises, drop the loop guard
        # so the parent process doesn't carry a stale marker, then re-raise
        # so the original failure isn't silently swallowed.
        child_env_marker_unset()
        raise


def child_env_marker_set() -> None:
    """Set the loop-guard env var. Extracted for test seams."""
    os.environ[_REEXEC_DONE_ENV] = "1"


def child_env_marker_unset() -> None:
    """Clear the loop-guard env var. Used when execvp fails so the parent
    process does not carry a stale marker into subsequent logic."""
    os.environ.pop(_REEXEC_DONE_ENV, None)


def _should_attempt_reexec(argv: Sequence[str] | None) -> bool:
    """Return True iff the current invocation matches the stale-group state."""
    if platform.system() != "Linux":
        return False
    if os.environ.get(_REEXEC_DONE_ENV) == "1":
        return False
    if os.environ.get(_REEXEC_DISABLE_ENV):
        return False

    args = list(argv) if argv is not None else sys.argv[1:]
    if args and args[0] in _SKIP_FIRST_ARGS:
        return False
    if len(args) >= 2 and args[0] == "sandbox" and args[1] in _SKIP_SANDBOX_ACTIONS:
        return False
    if (
        len(args) >= 3
        and args[0] == "sandbox"
        and (args[1], args[2]) in _SKIP_SANDBOX_NESTED_ACTIONS
    ):
        return False
    if len(args) >= 2 and args[0] == "browser" and args[1] in _SKIP_BROWSER_ACTIONS:
        return False
    if any(arg in _HELP_VERSION_FLAGS for arg in args):
        return False

    if not _KVM_DEV.exists():
        return False
    if os.access(_KVM_DEV, os.R_OK | os.W_OK):
        return False  # already accessible — nothing to do

    # The user must be listed as a member of the kvm group in /etc/group;
    # otherwise `sg kvm` would prompt for a password. We never want to
    # surface a password prompt mid-CLI.
    try:
        import grp  # POSIX-only; the platform check above guards macOS callers.
        import pwd

        kvm_members = set(grp.getgrnam(_KVM_GROUP).gr_mem)
        kvm_gid = grp.getgrnam(_KVM_GROUP).gr_gid
        current_user = pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, ImportError):
        return False

    if current_user not in kvm_members:
        return False  # genuine "not in group" case — don't paper over it
    # If kvm is already in our effective set but /dev/kvm is still denied,
    # something else is wrong (mode bits, ACLs, etc.). Don't re-exec; let
    # the doctor row surface the real problem.
    return kvm_gid not in os.getgroups()
