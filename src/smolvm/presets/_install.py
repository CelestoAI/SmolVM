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

"""Apply a preset to a running SmolVM guest over its control channel."""

from __future__ import annotations

import fnmatch
import getpass
import logging
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from smolvm.env import inject_env_vars
from smolvm.exceptions import SmolVMError
from smolvm.presets._git import GIT_HOST_CONFIGS, register_workspace_safe_directories

if TYPE_CHECKING:
    from smolvm.comm.base import CommChannel
    from smolvm.presets._types import Preset


logger = logging.getLogger(__name__)

# How long the install script may take. Apt + NodeSource setup + global
# npm install can run for a couple of minutes on a cold image.
_DEFAULT_INSTALL_TIMEOUT = 600

# Cap the wait on `security find-generic-password`. The first call may
# pop up a system dialog asking the user to grant access; if they walk
# away from the keyboard we don't want to hang sandbox provisioning
# indefinitely. After the user clicks "Always Allow" once, future calls
# return instantly.
_KEYCHAIN_LOOKUP_TIMEOUT = 60


def collect_host_env(preset: Preset) -> dict[str, str]:
    """Return env vars from os.environ that match preset.host_env_vars."""
    return {key: value for key in preset.host_env_vars if (value := os.environ.get(key))}


def apply_preset(
    ssh: CommChannel,
    preset: Preset,
    *,
    on_progress: Callable[[str], None] | None = None,
    install_timeout: int = _DEFAULT_INSTALL_TIMEOUT,
) -> dict[str, object]:
    """Apply *preset* to a guest reachable through a control channel.

    Order: copy host configs, inject env vars, then run the install
    script. Configs are placed before install runs so that the freshly
    installed CLI finds its config on first invocation.

    Returns:
        A summary dict (copied paths, injected env keys) suitable for
        logging or JSON output.
    """
    notify = on_progress or (lambda _msg: None)

    copied_configs = transfer_host_configs(
        ssh, preset, on_progress=on_progress, include_git_configs=True
    )

    # Keychain step runs after host_configs so a directory copy that
    # targets the same parent (e.g. ~/.claude → /root/.claude) cannot
    # tar-extract over a credential file we just wrote.
    extracted_secrets: list[str] = transfer_keychain_secrets(ssh, preset, on_progress=on_progress)

    env_vars = collect_host_env(preset)
    injected_keys: list[str] = []
    if env_vars:
        notify(f"Forwarding {len(env_vars)} environment variable(s)...")
        set_managed_env = getattr(ssh, "set_managed_env", None)
        supports = getattr(ssh, "supports", None)
        if callable(set_managed_env) and (
            callable(supports) and supports("env_managed", "env.managed") is True
        ):
            injected_keys = sorted(set_managed_env(env_vars).keys())
        else:
            injected_keys = inject_env_vars(ssh, env_vars)

    # Setup phase (apt + Node toolchain) runs before install_script so
    # the user sees two distinct progress steps instead of one opaque
    # "Installing..." line that stalls for the full duration.
    if preset.setup_script.strip():
        notify("Installing system packages...")
        _run_install_phase(ssh, preset, preset.setup_script, install_timeout, phase="setup")

    if preset.install_script.strip():
        notify(f"Installing {preset.name}...")
        _run_install_phase(ssh, preset, preset.install_script, install_timeout, phase="install")

    # Trust the workspace mounts so git stops refusing to operate on the
    # 9p-shared repo with "fatal: detected dubious ownership". Runs after
    # the install script because the upstream Ubuntu minimal cloudimg ships
    # without git; the bootstrap in NODE20_BOOTSTRAP is what actually puts
    # the binary on PATH.
    notify("Trusting workspace mount(s)...")
    register_workspace_safe_directories(ssh)

    return {
        "preset": preset.name,
        "copied_configs": copied_configs,
        "extracted_keychain_secrets": extracted_secrets,
        "injected_env_keys": injected_keys,
    }


def _run_install_phase(
    ssh: CommChannel,
    preset: Preset,
    script: str,
    install_timeout: int,
    *,
    phase: str,
) -> None:
    """Run a setup or install bash script and surface failures uniformly.

    *phase* tags the SmolVMError context (``"setup"`` or ``"install"``) so
    JSON consumers can tell which step failed without parsing the message.
    """
    command = f"bash -lc {shlex.quote(script)}"
    result = ssh.run(command, timeout=install_timeout, shell="raw")
    if not result.ok:
        stderr_tail = (result.stderr or "").strip().splitlines()[-20:]
        raise SmolVMError(
            f"Preset {preset.name!r} {phase} failed (exit {result.exit_code})",
            {
                "preset": preset.name,
                "phase": phase,
                "exit_code": result.exit_code,
                "stderr_tail": "\n".join(stderr_tail),
            },
        )


def _copy_to_guest(
    ssh: CommChannel,
    local: Path,
    guest_path: str,
    *,
    file_mode: int | None = None,
    transform: Callable[[bytes], bytes] | None = None,
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
) -> None:
    """Copy a host file or directory tree to *guest_path* inside the VM.

    *file_mode* applies only to single-file copies; directory copies
    inherit per-file modes from the tar archive. *transform*, when set,
    rewrites a single file's bytes before upload (directory copies have
    no single byte stream and reject a transform). *include_patterns*
    and *exclude_patterns* apply only to directory copies and match
    POSIX paths relative to the copied directory.
    """
    if local.is_file():
        if include_patterns or exclude_patterns:
            raise SmolVMError(
                f"Directory filters cannot be used with a file: {local}",
                {"host_path": str(local)},
            )
        _copy_file(ssh, local, guest_path, file_mode=file_mode, transform=transform)
        return
    if local.is_dir():
        if transform is not None:
            raise SmolVMError(
                f"transform is not supported for directory copies: {local}",
                {"host_path": str(local)},
            )
        _copy_dir(
            ssh,
            local,
            guest_path,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        return
    raise SmolVMError(
        f"Unsupported host path type (not file or directory): {local}",
        {"host_path": str(local)},
    )


def _copy_file(
    ssh: CommChannel,
    local: Path,
    guest_path: str,
    *,
    file_mode: int | None = None,
    transform: Callable[[bytes], bytes] | None = None,
) -> None:
    """Upload a single host file to *guest_path* via SFTP, mkdir parent first.

    SFTP ``put`` does not preserve mode and the SFTP server applies its
    own umask (typically 0644). When the source is a credential file
    (e.g. ``~/.git-credentials`` with plaintext OAuth tokens), pass
    *file_mode* to chmod the guest file after the upload so it does
    not land world-readable.

    When *transform* is set, the host file's bytes are passed through it
    and the result is uploaded instead of the original — staged in a
    0o600 host tempfile so a transformed credential file never sits
    world-readable on the host mid-upload.
    """
    parent = _posix_dirname(guest_path)
    if parent:
        result = ssh.run(f"mkdir -p -- {shlex.quote(parent)}", timeout=10)
        if not result.ok:
            raise SmolVMError(
                f"Failed to create guest directory {parent!r}",
                {"exit_code": result.exit_code, "stderr": result.stderr},
            )

    upload_src = local
    tmp_path: Path | None = None
    try:
        if transform is not None:
            transformed = transform(local.read_bytes())
            fd, tmp_name = tempfile.mkstemp(prefix=".smolvm-cfg-", suffix=".tmp")
            tmp_path = Path(tmp_name)
            # mkstemp creates the file 0o600; write via the fd to avoid a
            # world-readable window before upload.
            with os.fdopen(fd, "wb") as fp:
                fp.write(transformed)
            upload_src = tmp_path

        ssh.put_file(upload_src, guest_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    if file_mode is not None:
        chmod_cmd = f"chmod {file_mode:o} -- {shlex.quote(guest_path)}"
        result = ssh.run(chmod_cmd, timeout=5)
        if not result.ok:
            raise SmolVMError(
                f"Failed to chmod {guest_path!r} on guest",
                {"exit_code": result.exit_code, "stderr": result.stderr},
            )


def _copy_dir(
    ssh: CommChannel,
    local: Path,
    guest_path: str,
    *,
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
) -> None:
    """Tar a host directory tree and untar it into *guest_path* on the guest.

    Strips ownership (uid/gid/uname/gname) on every TarInfo so the guest
    extraction — which runs as root and defaults to ``--same-owner`` —
    does not preserve host uids (e.g. macOS ``501:20``). With host uids
    intact, ``/root/.ssh/id_ed25519`` would land owned by uid 501 and
    sshd would reject the key with "Bad owner or permissions". File
    modes (the 0o600 we care about for SSH keys) are untouched.
    """
    has_filters = bool(include_patterns or exclude_patterns)
    put_directory = getattr(ssh, "put_directory", None)
    supports = getattr(ssh, "supports", None)
    if (
        not has_filters
        and callable(put_directory)
        and callable(supports)
        and supports("dir_tar", "files.directory_tar") is True
    ):
        put_directory(local, guest_path)
        return

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        with tarfile.open(tmp_path, "w") as tf:
            tf.add(
                local,
                arcname=".",
                filter=_make_dir_tar_filter(include_patterns, exclude_patterns),
            )

        guest_tmp = f"/tmp/.smolvm-preset-{uuid4().hex}.tar"
        ssh.put_file(tmp_path, guest_tmp)
        cmd = (
            "set -e; "
            f"mkdir -p -- {shlex.quote(guest_path)}; "
            f"tar -xf {shlex.quote(guest_tmp)} -C {shlex.quote(guest_path)}; "
            f"rm -f -- {shlex.quote(guest_tmp)}"
        )
        result = ssh.run(cmd, timeout=60)
        if not result.ok:
            raise SmolVMError(
                f"Failed to extract config archive into {guest_path!r}",
                {"exit_code": result.exit_code, "stderr": result.stderr},
            )
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _strip_tar_owner(ti: tarfile.TarInfo) -> tarfile.TarInfo:
    """Reset every TarInfo's owner to root before it lands in the archive."""
    ti.uid = 0
    ti.gid = 0
    ti.uname = ""
    ti.gname = ""
    return ti


def _make_dir_tar_filter(
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
) -> Callable[[tarfile.TarInfo], tarfile.TarInfo | None]:
    """Build a tarfile filter that can include only selected directory entries."""
    includes = tuple(_normalize_dir_pattern(p) for p in include_patterns if p)
    excludes = tuple(_normalize_dir_pattern(p) for p in exclude_patterns if p)

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        rel = _tar_member_relative_name(ti.name)
        if rel and any(_matches_exclude_pattern(rel, pattern) for pattern in excludes):
            return None
        if includes and not _matches_include_or_parent(rel, ti.isdir(), includes):
            return None
        return _strip_tar_owner(ti)

    return _filter


def _tar_member_relative_name(name: str) -> str:
    """Normalize tar member names from ``./path`` to ``path``."""
    if name == ".":
        return ""
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/")


def _normalize_dir_pattern(pattern: str) -> str:
    """Normalize directory filter patterns to relative POSIX paths."""
    pattern = pattern.replace("\\", "/").strip()
    while pattern.startswith("./"):
        pattern = pattern[2:]
    return pattern.lstrip("/")


def _matches_exclude_pattern(rel: str, pattern: str) -> bool:
    """Return True when *pattern* excludes *rel* or one of its parents."""
    if not pattern:
        return False
    if pattern.endswith("/**"):
        base = pattern[:-3].rstrip("/")
        return rel == base or rel.startswith(f"{base}/")
    if "/" not in pattern:
        first = rel.split("/", 1)[0]
        basename = rel.rsplit("/", 1)[-1]
        return fnmatch.fnmatchcase(first, pattern) or fnmatch.fnmatchcase(basename, pattern)
    return fnmatch.fnmatchcase(rel, pattern)


def _matches_include_or_parent(
    rel: str,
    is_dir: bool,
    patterns: tuple[str, ...],
) -> bool:
    """Return True when *rel* is explicitly included or needed as a parent."""
    if not rel:
        return True
    if any(_matches_include_pattern(rel, pattern) for pattern in patterns):
        return True
    if not is_dir:
        return False
    rel_prefix = f"{rel}/"
    return any(
        pattern.startswith(rel_prefix) or _literal_pattern_prefix(pattern).startswith(rel_prefix)
        for pattern in patterns
    )


def _matches_include_pattern(rel: str, pattern: str) -> bool:
    """Return True when an include pattern selects this relative path."""
    if not pattern:
        return False
    if pattern.endswith("/**"):
        base = pattern[:-3].rstrip("/")
        return rel == base or rel.startswith(f"{base}/")
    if "/" not in pattern and "/" in rel:
        return False
    return fnmatch.fnmatchcase(rel, pattern)


def _literal_pattern_prefix(pattern: str) -> str:
    """Return the literal path prefix before the first glob metacharacter."""
    positions = [idx for marker in ("*", "?", "[") if (idx := pattern.find(marker)) != -1]
    if not positions:
        return pattern
    return pattern[: min(positions)]


def _posix_dirname(path: str) -> str:
    """Return the parent of a POSIX absolute path (no host-OS interpretation)."""
    if "/" not in path:
        return ""
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


def _extract_keychain_secret(service: str, *, account: str | None = None) -> str | None:
    """Return the password stored under *service* in the macOS keychain.

    When *account* is provided, scopes the lookup with ``-a <account>``
    so that an item under a different account (e.g. claude-code's
    ``acct=root`` MCP-token entry) doesn't shadow the one we want.

    Returns ``None`` (never raises) on:
      - non-macOS hosts,
      - missing ``security`` binary,
      - missing keychain entry,
      - the user denying access at the system prompt,
      - the lookup exceeding ``_KEYCHAIN_LOOKUP_TIMEOUT``.

    A ``None`` return is the signal for the caller to fall through —
    the user can still authenticate inside the guest with ``/login`` or
    by setting the harness's API-key env var.
    """
    if sys.platform != "darwin":
        return None
    cmd = ["security", "find-generic-password", "-s", service]
    if account is not None:
        cmd.extend(["-a", account])
    cmd.append("-w")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_KEYCHAIN_LOOKUP_TIMEOUT,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    # `security -w` appends a single trailing newline; strip it without
    # touching newlines inside the value (none in practice for the
    # JSON blob Claude Code stores, but safer this way).
    value = result.stdout
    if value.endswith("\n"):
        value = value[:-1]
    return value


def _write_secret_to_guest(ssh: CommChannel, content: str, guest_path: str, file_mode: int) -> None:
    """Stage *content* in a 0o600 host tempfile, SFTP it, then chmod on the guest."""
    parent = _posix_dirname(guest_path)
    if parent:
        result = ssh.run(f"mkdir -p -- {shlex.quote(parent)}", timeout=10)
        if not result.ok:
            raise SmolVMError(
                f"Failed to create guest directory {parent!r}",
                {"exit_code": result.exit_code, "stderr": result.stderr},
            )

    fd, tmp_name = tempfile.mkstemp(prefix=".smolvm-secret-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        # mkstemp already creates the file with mode 0o600; write via the
        # returned fd to avoid a brief world-readable window.
        with os.fdopen(fd, "w") as fp:
            fp.write(content)
        ssh.put_file(tmp_path, guest_path)
        chmod_cmd = f"chmod {file_mode:o} -- {shlex.quote(guest_path)}"
        result = ssh.run(chmod_cmd, timeout=5)
        if not result.ok:
            raise SmolVMError(
                f"Failed to chmod {guest_path!r} on guest",
                {"exit_code": result.exit_code, "stderr": result.stderr},
            )
    finally:
        tmp_path.unlink(missing_ok=True)


def transfer_host_configs(
    ssh: CommChannel,
    preset: Preset,
    *,
    on_progress: Callable[[str], None] | None = None,
    include_git_configs: bool = True,
) -> list[str]:
    """Copy the preset's host config files/dirs into the guest.

    Runs only the config-copy step from :func:`apply_preset` — no
    keychain extraction, no env injection, no install scripts. The
    published-image path reuses this so a sandbox booted from a
    pre-baked image still receives ``~/.claude.json`` and ``~/.claude``
    (the interactive CLI reads ``~/.claude.json`` to skip its login
    screen; the keychain token alone only satisfies headless ``-p``).

    When *include_git_configs* is True the git/ssh credential set
    (:data:`GIT_HOST_CONFIGS`) is appended so ``git``/``gh`` work in the
    guest. The published-image path passes False — it only needs the
    preset's own configs.

    Missing host files are skipped silently unless marked ``required``,
    which raises :class:`SmolVMError`. Returns the guest paths written.
    """
    notify = on_progress or (lambda _msg: None)

    copied_configs: list[str] = []
    git_configs = GIT_HOST_CONFIGS if include_git_configs else ()
    # Git configs piggyback on host_configs so dir copies (e.g. ~/.ssh)
    # preserve 0o600 modes via tar and the run summary surfaces them
    # under the existing copied_configs key.
    all_configs = (*preset.host_configs, *git_configs)
    if all_configs:
        notify("Copying host credentials (gitconfig, ssh keys, CLI configs)...")
    for cfg in all_configs:
        local = Path(cfg.host_path).expanduser()
        if not local.exists():
            if cfg.required:
                raise SmolVMError(
                    f"The {preset.name} sandbox needs the file '{local}' on your "
                    "machine, but it isn't there. Restore it and start the sandbox "
                    "again.",
                    {"preset": preset.name, "host_path": str(local)},
                )
            logger.debug("Skipping missing host config: %s", local)
            continue
        _copy_to_guest(
            ssh,
            local,
            cfg.guest_path,
            file_mode=cfg.file_mode,
            transform=cfg.transform,
            include_patterns=cfg.include_patterns,
            exclude_patterns=cfg.exclude_patterns,
        )
        copied_configs.append(cfg.guest_path)
    return copied_configs


def transfer_keychain_secrets(
    ssh: CommChannel,
    preset: Preset,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> list[str]:
    """Extract macOS keychain secrets and write them into the guest.

    Runs only the keychain step from :func:`apply_preset` — no config
    copies, no env injection, no install scripts. Used by the published-
    image path where the CLI is already baked in and only credential
    transfer is needed.

    Returns a list of guest paths where secrets were written (empty when
    not on macOS, when the keychain item is missing, or when the user
    denies access).
    """
    notify = on_progress or (lambda _msg: None)
    extracted: list[str] = []
    for secret in preset.host_keychain_secrets:
        account = secret.account if secret.account is not None else getpass.getuser()
        plaintext = _extract_keychain_secret(secret.service, account=account)
        if plaintext is None:
            logger.debug(
                "Skipping unavailable keychain secret: service=%s account=%s",
                secret.service,
                account,
            )
            continue
        notify(f"Copying {secret.service} from macOS keychain → {secret.guest_path}")
        _write_secret_to_guest(ssh, plaintext, secret.guest_path, secret.file_mode)
        extracted.append(secret.guest_path)
    return extracted


__all__ = [
    "apply_preset",
    "collect_host_env",
    "transfer_host_configs",
    "transfer_keychain_secrets",
]
