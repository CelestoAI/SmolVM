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

"""Environment variable injection for Windows guest VMs.

Mirror of :mod:`smolvm.env` for Windows. Uses PowerShell's
``[Environment]::SetEnvironmentVariable(name, value, 'User')`` API
instead of writing to ``/etc/profile.d/`` — that's the standard Windows
way to persist user-level env vars (writes the HKCU\\Environment
registry key and broadcasts ``WM_SETTINGCHANGE`` so new processes
pick up the change).

Because the Windows registry has no native "namespace" concept like
the Linux ``/etc/profile.d/smolvm_env.sh`` single-file pattern, we
track which keys SmolVM has managed via a sentinel registry value
(``SMOLVM_ENV_MANAGED_KEYS``, comma-separated). Read/remove then scope
to that list so SmolVM never accidentally touches an env var the user
set up themselves outside SmolVM.

Variables are picked up by **subsequent** SSH sessions / spawned
processes, not the current one. ``vm.run()`` opens a fresh SSH
session per call, so a `vm.set_env_vars({...}); vm.run("...")` pair
will see the new vars on the second call.
"""

from __future__ import annotations

import json
import logging

from smolvm.env import validate_env_key
from smolvm.exceptions import SmolVMError
from smolvm.ssh import SSHClient

logger = logging.getLogger(__name__)

# Sentinel registry value that holds the comma-separated list of env-var
# names SmolVM has set. Lives in HKCU\Environment alongside the values it
# tracks, so a Windows user inspecting their environment can see who set
# which vars.
_MANAGED_KEYS_SENTINEL = "SMOLVM_ENV_MANAGED_KEYS"


def _validate_windows_env_key(key: str) -> None:
    """Validate *key* with the shared rules + Windows-specific reserved-name check.

    The sentinel ``SMOLVM_ENV_MANAGED_KEYS`` is how SmolVM tracks which
    env vars it has set — letting a caller use that name as a regular
    env var would corrupt the bookkeeping and SmolVM would no longer
    know which vars it owns. Reserve it loudly here.
    """
    validate_env_key(key)
    if key == _MANAGED_KEYS_SENTINEL:
        raise ValueError(
            f"{_MANAGED_KEYS_SENTINEL!r} is reserved for SmolVM's internal "
            "bookkeeping of managed Windows env vars and cannot be set or "
            "removed via the public API. Choose a different variable name."
        )


def _ps_single_quote(value: str) -> str:
    """Escape *value* for a PowerShell single-quoted string literal.

    PowerShell's only escape inside ``'...'`` is doubling the embedded
    single quote (``''``). Everything else is literal — backticks,
    dollar signs, backslashes — which is exactly what we want for
    setting env-var values verbatim.
    """
    return value.replace("'", "''")


def _set_user_var_ps(name: str, value: str) -> str:
    """Return a PowerShell statement that sets ``HKCU\\Environment\\name``."""
    return (
        f"[Environment]::SetEnvironmentVariable("
        f"'{_ps_single_quote(name)}', '{_ps_single_quote(value)}', 'User')"
    )


def _clear_user_var_ps(name: str) -> str:
    """Return a PowerShell statement that deletes ``HKCU\\Environment\\name``."""
    return f"[Environment]::SetEnvironmentVariable('{_ps_single_quote(name)}', $null, 'User')"


def _run_ps(ssh: SSHClient, script: str, *, timeout: int = 20) -> str:
    """Run a multi-statement PowerShell script over the SSH client.

    ``ssh.run`` already wraps in ``powershell.exe -NoProfile
    -EncodedCommand`` for Windows guests, so the whole script (which
    may contain quotes, ``$``, multi-line — anything) survives the
    cmd.exe layer Windows OpenSSH routes through.
    """
    result = ssh.run(script, timeout=timeout, shell="login")
    if result.exit_code != 0:
        raise SmolVMError(
            "PowerShell env-var command failed",
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            },
        )
    return result.stdout


def _read_managed_keys(ssh: SSHClient) -> list[str]:
    """Read the SmolVM-managed-keys sentinel from the guest registry."""
    out = _run_ps(
        ssh,
        f"$v = [Environment]::GetEnvironmentVariable('{_MANAGED_KEYS_SENTINEL}', 'User'); "
        "if ($v) { Write-Output $v }",
    )
    text = out.strip()
    if not text:
        return []
    # Filter empties (split of empty string gives [''])
    return [k for k in (s.strip() for s in text.split(",")) if k]


def _write_managed_keys(ssh: SSHClient, keys: list[str]) -> None:
    """Persist the SmolVM-managed-keys sentinel in the guest registry."""
    joined = ",".join(sorted(keys))
    if joined:
        _run_ps(ssh, _set_user_var_ps(_MANAGED_KEYS_SENTINEL, joined))
    else:
        # Empty list — remove the sentinel entirely.
        _run_ps(ssh, _clear_user_var_ps(_MANAGED_KEYS_SENTINEL))


def inject_env_vars(
    ssh: SSHClient,
    env_vars: dict[str, str],
    *,
    merge: bool = True,
) -> list[str]:
    """Persist *env_vars* into the Windows guest's user-level environment.

    When *merge* is ``True`` (the default), existing SmolVM-managed
    vars are preserved and the new ones overwrite duplicates. When
    *merge* is ``False``, every previously-managed var is cleared first
    so the final state matches *env_vars* exactly.

    Args:
        ssh: Connected SSH client for the Windows guest.
        env_vars: Mapping of variable names to values.
        merge: If True (default), merge with existing SmolVM-managed
            vars; if False, replace them entirely.

    Returns:
        Sorted list of variable names that are now set.

    Raises:
        SmolVMError: If any of the PowerShell commands fail.
        ValueError: If any key is not a valid identifier.
    """
    if not env_vars:
        return []

    for key in env_vars:
        _validate_windows_env_key(key)

    existing_keys = _read_managed_keys(ssh)
    if merge:
        final_keys = sorted(set(existing_keys) | set(env_vars.keys()))
    else:
        # Replace mode: clear previously-managed keys that aren't in the
        # new set so the final state matches env_vars exactly.
        to_clear = sorted(set(existing_keys) - set(env_vars.keys()))
        if to_clear:
            clear_script = "\n".join(_clear_user_var_ps(k) for k in to_clear)
            _run_ps(ssh, clear_script)
        final_keys = sorted(env_vars.keys())

    # Set all the new/updated values in one PowerShell session.
    set_script = "\n".join(_set_user_var_ps(k, v) for k, v in sorted(env_vars.items()))
    _run_ps(ssh, set_script)

    _write_managed_keys(ssh, final_keys)
    logger.info(
        "Windows env injection: set %d var(s): %s",
        len(env_vars),
        ", ".join(sorted(env_vars)),
    )
    return final_keys


def read_env_vars(ssh: SSHClient) -> dict[str, str]:
    """Read SmolVM-managed environment variables from the Windows guest.

    Returns only the vars SmolVM has set (tracked via the
    ``SMOLVM_ENV_MANAGED_KEYS`` sentinel) — not the user's entire
    HKCU\\Environment. Mirrors the Linux side's "only what we manage"
    semantic.

    Values may contain newlines, tabs, quotes, or any other character —
    we round-trip them through a PowerShell hashtable serialized with
    ``ConvertTo-Json``, then ``json.loads`` host-side. The line-split
    approach previously used here truncated multi-line values at the
    first ``\\n``.
    """
    managed = _read_managed_keys(ssh)
    if not managed:
        return {}

    # Build a PowerShell hashtable of managed_key -> value, then emit
    # it as a single JSON blob. -Compress keeps the payload to one line
    # which sidesteps any cmd.exe line-buffering edge cases, and -Depth 5
    # is plenty for the flat string->string mapping we produce.
    lines = ["$h = @{}"]
    for k in managed:
        quoted = _ps_single_quote(k)
        lines.append(
            f"$v = [Environment]::GetEnvironmentVariable('{quoted}', 'User'); "
            f"if ($null -ne $v) {{ $h['{quoted}'] = $v }}"
        )
    lines.append("$h | ConvertTo-Json -Compress -Depth 5")
    out = _run_ps(ssh, "\n".join(lines))

    payload = out.strip()
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SmolVMError(
            "Failed to parse JSON env-var payload from Windows guest",
            {"payload": payload[:500], "error": str(exc)},
        ) from exc

    # ConvertTo-Json on a single-entry hashtable still emits an object,
    # not a scalar — so we always expect a dict. Defensive: if PowerShell
    # somehow emits a non-object (older versions can collapse), coerce.
    if not isinstance(parsed, dict):
        return {}
    # Coerce every value to str — PowerShell may emit ints/bools if a
    # value happens to be numeric-looking, but our contract is str->str.
    return {str(k): str(v) for k, v in parsed.items()}


def remove_env_vars(ssh: SSHClient, keys: list[str]) -> dict[str, str]:
    """Remove the named env vars from the Windows guest (scoped to managed)."""
    if not keys:
        return {}

    # Validate up front so the sentinel can never be cleared via the
    # public API (which would orphan the keys it tracks).
    for key in keys:
        _validate_windows_env_key(key)

    current = read_env_vars(ssh)
    removed: dict[str, str] = {k: current[k] for k in keys if k in current}
    if not removed:
        return {}

    clear_script = "\n".join(_clear_user_var_ps(k) for k in removed)
    _run_ps(ssh, clear_script)

    # Update the managed-keys sentinel to drop the removed names.
    remaining = sorted(set(_read_managed_keys(ssh)) - set(removed))
    _write_managed_keys(ssh, remaining)

    logger.info(
        "Windows env injection: removed %d var(s): %s",
        len(removed),
        ", ".join(sorted(removed)),
    )
    return removed
