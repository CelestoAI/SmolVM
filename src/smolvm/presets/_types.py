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

"""Type definitions for SmolVM agent-harness presets."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostConfigCopy:
    """A host file or directory to copy into the guest at preset apply time.

    Attributes:
        host_path: Source path on the host. Leading ``~`` is expanded.
        guest_path: Destination path inside the guest (absolute).
        required: If True, raise an error when the host path is missing.
            If False (default), the copy is silently skipped.
        file_mode: Octal permission bits to apply to the guest file
            after upload. Single-file copies only — directory copies
            inherit modes from the tar archive. Use this for credential
            files (e.g. ``~/.git-credentials``) that the SFTP server's
            umask would otherwise leave world-readable. ``None``
            (default) leaves the SFTP-applied mode in place.
    """

    host_path: str
    guest_path: str
    required: bool = False
    file_mode: int | None = None


@dataclass(frozen=True)
class HostKeychainSecret:
    """A macOS keychain secret extracted on the host and written into the guest.

    Some CLIs store their auth tokens in the macOS Keychain rather than
    on disk (Claude Code is one example: on macOS the OAuth tokens live
    in a keychain item, while on Linux the same CLI reads them from
    ``~/.claude/.credentials.json``). When the user copies the on-disk
    config into a Linux guest, the credentials don't come along — the
    guest reports "Not logged in" even though the host is signed in.

    The applier looks up the keychain item by *service* (and *account*
    when set) via ``security find-generic-password -s <service> [-a
    <account>] -w`` and writes the returned password verbatim to
    *guest_path* with *file_mode*.

    Outside macOS — and when the keychain entry is missing or access
    is denied at the system prompt — the step is a silent no-op so the
    user can still authenticate inside the guest with ``/login`` or by
    setting the harness's API-key env var.

    Attributes:
        service: Keychain service name (the ``-s`` argument).
        guest_path: Destination path inside the guest (absolute).
        account: Keychain account name (the ``-a`` argument). When
            ``None``, the applier auto-detects via ``getpass.getuser()``
            — the macOS login user, which is what Claude Code uses for
            its OAuth keychain entry. Set explicitly only if the entry
            you need is filed under a different account; multiple
            entries can share a service name (e.g. one per account),
            and ``-s`` alone returns whichever the keychain hits first,
            which may not be the right one.
        file_mode: Octal permission bits applied to the guest file.
            Defaults to ``0o600`` because credentials files should not
            be world-readable.
    """

    service: str
    guest_path: str
    account: str | None = None
    file_mode: int = 0o600


@dataclass(frozen=True)
class Preset:
    """A reusable agent-harness blueprint applied to a fresh sandbox.

    Presets describe a common pattern: boot an SSH-capable VM, copy a
    handful of host config files in, set a few env vars from the host
    environment, and run an idempotent install script over SSH.

    Attributes:
        name: CLI-facing identifier (e.g. ``"codex"``).
        aliases: Extra names that resolve to this preset on the CLI
            (e.g. ``("claude",)`` for ``claude-code``). Argparse routes
            them to the same parser; the canonical ``name`` is what
            ``args.preset_name`` carries.
        summary: One-line description for ``--help`` output.
        install_script: Bash script run via ``ssh.run`` after boot.
            Should be idempotent and self-contained.
        host_env_vars: Names of host environment variables. Each one
            present and non-empty on the host is forwarded into the
            guest as a persistent env var.
        host_configs: Files or directories to copy from host to guest.
        host_keychain_secrets: macOS keychain items extracted on the
            host and written into the guest. Skipped silently on
            non-macOS hosts and when the entry is missing or access is
            denied.
        default_mem_mib: Memory bump versus the OS default.
        default_disk_mib: Disk bump versus the OS default.
    """

    name: str
    summary: str
    install_script: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    host_env_vars: tuple[str, ...] = field(default_factory=tuple)
    host_configs: tuple[HostConfigCopy, ...] = field(default_factory=tuple)
    host_keychain_secrets: tuple[HostKeychainSecret, ...] = field(default_factory=tuple)
    default_mem_mib: int = 2048
    default_disk_mib: int = 8192
    launch_command: str | None = None
    """Guest command run when the user attaches after install — e.g. ``"codex"``."""
