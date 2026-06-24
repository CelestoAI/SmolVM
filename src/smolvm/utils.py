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

"""Shared utility functions for SmolVM SDK."""

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from smolvm.exceptions import SmolVMError

logger = logging.getLogger(__name__)

RUNTIME_PRIVILEGE_SETUP_HINT = "Configure non-interactive runtime privileges once:\n  smolvm setup"


def _is_sudo_non_interactive_error(stderr: str) -> bool:
    """Return True when stderr indicates sudo auth/tty/sudoers issues."""
    text = stderr.lower()
    patterns = (
        "a password is required",
        "no tty present",
        "a terminal is required",
        "not in the sudoers file",
        "may not run sudo",
    )
    return any(pattern in text for pattern in patterns)


def run_command(
    cmd: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    use_sudo: bool = True,
    timeout: int = 30,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a system command with optional sudo.

    Args:
        cmd: Command and arguments.
        check: Raise on non-zero exit code.
        capture_output: Capture stdout/stderr.
        use_sudo: Prefix with sudo if not root.
        timeout: Command timeout in seconds.
        input: Optional input string to pass to stdin.

    Returns:
        CompletedProcess result.

    Raises:
        SmolVMError: If command fails or times out.
    """
    if cmd is None or len(cmd) == 0:
        raise ValueError("cmd cannot be empty")

    full_cmd = list(cmd)
    if use_sudo and os.geteuid() != 0:
        # Never prompt interactively in SDK runtime paths.
        full_cmd = ["sudo", "-n", *full_cmd]

    logger.debug("Running command: %s", " ".join(full_cmd))

    try:
        start = time.monotonic()
        result = subprocess.run(
            full_cmd,
            check=check,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            input=input,
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        # Log at INFO so timing data is visible in normal operation.
        # The base command (e.g. "ip", "nft") is extracted for easy
        # histogram grouping in profiling/analysis.
        base_cmd = cmd[0] if cmd else "unknown"
        logger.info("CMD %-10s %.1fms", base_cmd, elapsed_ms)
        return result
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        if full_cmd[:2] == ["sudo", "-n"] and _is_sudo_non_interactive_error(stderr):
            raise SmolVMError(
                "Missing non-interactive sudo privileges for SmolVM runtime command.\n"
                f"Command: {' '.join(full_cmd)}\n"
                f"{RUNTIME_PRIVILEGE_SETUP_HINT}\n"
                f"sudo stderr: {stderr}"
            ) from e
        raise SmolVMError(f"Command failed: {' '.join(full_cmd)}\nstderr: {e.stderr}") from e
    except subprocess.TimeoutExpired as e:
        raise SmolVMError(f"Command timed out: {' '.join(full_cmd)}") from e


async def async_run_command(
    cmd: Sequence[str],
    *,
    check: bool = True,
    use_sudo: bool = True,
    timeout: int = 30,
    input: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Async version of :func:`run_command`.

    Uses ``asyncio.create_subprocess_exec`` for non-blocking execution.
    Same sudo prefixing, error handling, and timeout behaviour.
    """
    import asyncio

    if cmd is None or len(cmd) == 0:
        raise ValueError("cmd cannot be empty")

    full_cmd = list(cmd)
    if use_sudo and os.geteuid() != 0:
        full_cmd = ["sudo", "-n", *full_cmd]

    logger.debug("Running async command: %s", " ".join(full_cmd))

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input else asyncio.subprocess.DEVNULL,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=input.encode() if input else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SmolVMError(f"Command timed out: {' '.join(full_cmd)}") from None

    elapsed_ms = (time.monotonic() - start) * 1000
    base_cmd = cmd[0] if cmd else "unknown"
    logger.info("CMD %-10s %.1fms (async)", base_cmd, elapsed_ms)

    stdout_str = stdout_bytes.decode() if stdout_bytes else ""
    stderr_str = stderr_bytes.decode() if stderr_bytes else ""
    returncode = proc.returncode or 0

    if check and returncode != 0:
        if full_cmd[:2] == ["sudo", "-n"] and _is_sudo_non_interactive_error(stderr_str):
            raise SmolVMError(
                "Missing non-interactive sudo privileges for SmolVM runtime command.\n"
                f"Command: {' '.join(full_cmd)}\n"
                f"{RUNTIME_PRIVILEGE_SETUP_HINT}\n"
                f"sudo stderr: {stderr_str.strip()}"
            )
        raise SmolVMError(f"Command failed: {' '.join(full_cmd)}\nstderr: {stderr_str}")

    return subprocess.CompletedProcess(
        args=full_cmd,
        returncode=returncode,
        stdout=stdout_str,
        stderr=stderr_str,
    )


def which(binary: str) -> Path | None:
    """Find a binary on the system PATH.

    Args:
        binary: Name of the binary to find.

    Returns:
        Path to the binary, or None if not found.
    """
    if not binary:
        raise ValueError("binary name cannot be empty")

    result = shutil.which(binary)
    return Path(result) if result else None


def ensure_ssh_key(key_dir: Path | None = None) -> tuple[Path, Path]:
    """Ensure an ED25519 SSH key pair exists for the current user.

    Args:
        key_dir: Directory to store keys. Defaults to ``~/.smolvm/keys``.

    Returns:
        Tuple of (private_key_path, public_key_path).
    """
    sudo_uid: int | None = None
    sudo_gid: int | None = None

    if key_dir is None:
        user_home = Path.home()

        # If running as root via sudo, use the real user's home directory.
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user:
            import pwd

            try:
                pw = pwd.getpwnam(sudo_user)
                user_home = Path(pw.pw_dir)
                sudo_uid = pw.pw_uid
                sudo_gid = pw.pw_gid
            except KeyError:
                pass  # Fallback to root's home if user lookup fails.

        base_dir = user_home / ".smolvm"
        key_dir = base_dir / "keys"

    key_dir = Path(key_dir)
    if not key_dir.exists():
        key_dir.mkdir(parents=True, exist_ok=True)
        if sudo_uid is not None and sudo_gid is not None:
            os.chown(key_dir, sudo_uid, sudo_gid)

    private_key = key_dir / "id_ed25519"
    public_key = key_dir / "id_ed25519.pub"

    # Check if keys exist.
    if private_key.exists() and public_key.exists():
        # Ensure correct ownership if we are sudo.
        if sudo_uid is not None and sudo_gid is not None:
            try:
                os.chown(private_key, sudo_uid, sudo_gid)
                os.chown(public_key, sudo_uid, sudo_gid)
            except OSError:
                pass
        return private_key, public_key

    logger.info("Generating new SSH key pair at %s...", key_dir)
    subprocess.run(
        [
            "ssh-keygen",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(private_key),
            "-C",
            "smolvm-auto",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Fix ownership of generated keys.
    if sudo_uid is not None and sudo_gid is not None:
        os.chown(private_key, sudo_uid, sudo_gid)
        os.chown(public_key, sudo_uid, sudo_gid)

    return private_key, public_key
