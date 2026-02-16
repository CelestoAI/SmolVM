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

"""Environment variable injection for SmolVM guests.

Provides helpers to inject, read, and remove environment variables
inside a running microVM via SSH.  Variables are persisted to
``/etc/profile.d/smolvm_env.sh`` so that every new login shell
sources them automatically.

All file writes are **atomic** (write to tmp → ``mv`` into place) to
prevent partial updates on failure.
"""

import base64
import logging
import re
import shlex

from smolvm.exceptions import SmolVMError
from smolvm.ssh import SSHClient

logger = logging.getLogger(__name__)

# Path inside the guest where env vars are persisted.
ENV_FILE = "/etc/profile.d/smolvm_env.sh"

# Regex for valid POSIX shell identifier (used as env var key).
_VALID_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def validate_env_key(key: str) -> None:
    """Raise ``ValueError`` if *key* is not a valid shell identifier."""
    if not key:
        raise ValueError("Environment variable key cannot be empty")
    if not _VALID_KEY_RE.match(key):
        raise ValueError(
            f"Invalid environment variable key: {key!r}. Keys must match [A-Za-z_][A-Za-z0-9_]*"
        )


def _shell_quote(value: str) -> str:
    """Quote a value for safe embedding in a shell script.

    Uses ``shlex.quote`` which wraps in single quotes and escapes
    embedded single quotes (``'`` → ``'\\''``).
    """
    return shlex.quote(value)


def build_env_script(env_vars: dict[str, str]) -> str:
    """Generate a shell script that exports the given variables.

    Args:
        env_vars: Mapping of variable names to values.

    Returns:
        A string suitable for writing to ``/etc/profile.d/*.sh``.

    Raises:
        ValueError: If any key is not a valid shell identifier.
    """
    if not env_vars:
        return "# SmolVM environment variables (empty)\n"

    lines = ["#!/bin/sh", "# SmolVM managed environment variables", ""]
    for key, value in sorted(env_vars.items()):
        validate_env_key(key)
        lines.append(f"export {key}={_shell_quote(value)}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def inject_env_vars(
    ssh: SSHClient,
    env_vars: dict[str, str],
    *,
    merge: bool = True,
) -> list[str]:
    """Write environment variables into the guest VM.

    The file is written atomically (tmp + ``mv``).  When *merge* is
    ``True`` (the default), existing variables are preserved and the
    new ones are merged on top (overwriting duplicates).

    Args:
        ssh: Connected SSH client.
        env_vars: Variables to set.
        merge: If True, merge with existing vars. If False, replace entirely.

    Returns:
        Sorted list of variable names that were written.

    Raises:
        SmolVMError: If the SSH command fails.
        ValueError: If any key is invalid.
    """
    if not env_vars:
        return []

    # Validate all keys upfront before touching the guest.
    for key in env_vars:
        validate_env_key(key)

    if merge:
        existing = read_env_vars(ssh)
        existing.update(env_vars)
        final_vars = existing
    else:
        final_vars = dict(env_vars)

    script_content = build_env_script(final_vars)
    result = _atomic_write(ssh, script_content)
    if not result.ok:
        raise SmolVMError(
            f"Failed to inject environment variables: {result.stderr.strip()}",
            {"exit_code": result.exit_code},
        )

    keys = sorted(final_vars.keys())
    logger.info("Injected %d env var(s): %s", len(keys), ", ".join(keys))
    return keys


def _atomic_write(ssh: SSHClient, content: str) -> "CommandResult":
    """Write *content* to the env file atomically inside the guest.

    Uses **base64 transport** to avoid any shell-interpretation of the
    payload (heredoc delimiter injection, quote escaping, etc.).
    A ``mktemp``-generated temp file prevents symlink attacks, and a
    ``trap`` ensures the temp file is cleaned up on any failure.
    """
    from smolvm.types import CommandResult  # avoid circular at module level

    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    cmd = (
        "set -e; "
        "_t=$(mktemp /tmp/.smolvm_env.XXXXXXXXXX); "
        "trap 'rm -f \"$_t\"' EXIT; "
        f"printf '%s' '{b64}' | base64 -d > \"$_t\"; "
        f'chmod 0644 "$_t"; '
        f'mv "$_t" {ENV_FILE}'
    )
    return ssh.run(cmd, timeout=10)


def read_env_vars(ssh: SSHClient) -> dict[str, str]:
    """Read current SmolVM-managed environment variables from the guest.

    Parses ``/etc/profile.d/smolvm_env.sh`` for ``export KEY=VALUE``
    lines.  Uses ``shlex`` in POSIX mode to correctly handle all
    quoting styles produced by ``shlex.quote``.

    Args:
        ssh: Connected SSH client.

    Returns:
        Dict of key-value pairs.  Empty dict if the file doesn't exist.
    """
    result = ssh.run(f"cat {ENV_FILE} 2>/dev/null || true", timeout=10)
    if not result.ok:
        return {}

    env_vars: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        # Format: export KEY='value' or export KEY=value
        rest = line[len("export ") :]
        eq_idx = rest.find("=")
        if eq_idx < 0:
            continue
        key = rest[:eq_idx]
        raw_value = rest[eq_idx + 1 :]
        # Use a shlex lexer configured for single-token extraction.
        # This handles 'val'\''ue' style quoting correctly and treats
        # the entire remainder as one value (even if it contains spaces
        # inside quotes).
        try:
            lex = shlex.shlex(raw_value, posix=True)
            lex.whitespace_split = True
            tokens = list(lex)
            value = tokens[0] if tokens else ""
        except ValueError:
            logger.warning("Skipping malformed env line: %s", line)
            continue
        env_vars[key] = value

    return env_vars


def remove_env_vars(ssh: SSHClient, keys: list[str]) -> dict[str, str]:
    """Remove environment variables from the guest.

    Reads the current file, removes the specified keys, and rewrites
    the file atomically.

    Args:
        ssh: Connected SSH client.
        keys: Variable names to remove.

    Returns:
        Dict of variables that were actually removed (key → old value).
        Keys not found in the file are silently ignored.
    """
    if not keys:
        return {}

    current = read_env_vars(ssh)
    removed: dict[str, str] = {}
    for key in keys:
        if key in current:
            removed[key] = current.pop(key)

    if not removed:
        return {}

    # Rewrite without the removed keys.
    script_content = build_env_script(current)
    result = _atomic_write(ssh, script_content)
    if not result.ok:
        raise SmolVMError(
            f"Failed to update environment file: {result.stderr.strip()}",
            {"exit_code": result.exit_code},
        )

    logger.info("Removed %d env var(s): %s", len(removed), ", ".join(sorted(removed)))
    return removed
