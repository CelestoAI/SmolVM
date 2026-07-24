# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from smolvm.exceptions import SmolVMError
from smolvm.macos.desktop import open_desktop
from smolvm.macos.lume import LumeDriver
from smolvm.macos.models import MacOSInstallRequest, MacOSRunRequest
from smolvm.types import DesktopEndpoint, WorkspaceMount


@pytest.fixture
def fake_lume(tmp_path: Path) -> Path:
    binary = tmp_path / "lume"
    binary.write_text(
        """#!/usr/bin/env python3
import json
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print("0.4.0")
elif args and args[0] == "get":
    print(json.dumps([{
        "name": args[1],
        "os": "macOS",
        "cpuCount": 4,
        "memorySize": 8589934592,
        "diskSize": {"allocated": 1024, "total": 85899345920},
        "display": "1440x900",
        "status": "running",
        "provisioningOperation": None,
        "vncUrl": "vnc://:secret@127.0.0.1:5901",
        "ipAddress": "192.168.64.3",
        "sshAvailable": True,
        "locationName": "test",
        "sharedDirectories": [],
        "networkMode": "nat",
        "downloadProgress": None,
    }]))
elif args and args[0] == "run":
    print("Desktop vnc://:secret@127.0.0.1:5901", flush=True)
    time.sleep(30)
else:
    sys.exit(0)
"""
    )
    binary.chmod(0o755)
    return binary


def test_lume_install_uses_explicit_resource_defaults(fake_lume: Path, tmp_path: Path) -> None:
    driver = LumeDriver(fake_lume)
    completed = subprocess.CompletedProcess([], 0)
    with patch("smolvm.macos.lume.subprocess.run", return_value=completed) as run:
        driver.install_base_image(
            MacOSInstallRequest(name="macos-latest", storage_path=tmp_path),
            log_path=tmp_path / "build.log",
        )

    command = run.call_args.args[0]
    assert command[command.index("--cpu") + 1] == "4"
    assert command[command.index("--memory") + 1] == "8192MB"
    assert command[command.index("--disk-size") + 1] == "80GB"
    assert command[command.index("--display") + 1] == "1440x900"
    assert (tmp_path / "build.log").stat().st_mode & 0o777 == 0o600


def test_lume_driver_parses_machine_details(fake_lume: Path, tmp_path: Path) -> None:
    driver = LumeDriver(fake_lume)

    assert driver.version() == "0.4.0"
    details = driver.inspect("mac-test", storage_path=tmp_path)

    assert details.name == "mac-test"
    assert details.status == "running"
    assert details.ip_address == "192.168.64.3"
    assert details.vnc_url == "vnc://:secret@127.0.0.1:5901"


def test_lume_driver_starts_until_loopback_display_is_ready(
    fake_lume: Path, tmp_path: Path
) -> None:
    driver = LumeDriver(fake_lume)
    shared = tmp_path / "shared"
    shared.mkdir()
    request = MacOSRunRequest(
        name="mac-test",
        storage_path=tmp_path,
        workspace_mounts=(WorkspaceMount(host_path=shared),),
    )

    process, launch = driver.start(request, log_path=tmp_path / "vm.log", timeout=2)
    try:
        assert process.poll() is None
        assert launch.pid == process.pid
        assert launch.display == DesktopEndpoint(host="127.0.0.1", port=5901)
        assert launch.ip_address == "192.168.64.3"
        assert launch.vnc_password == "secret"
        assert (tmp_path / "vm.log").stat().st_mode & 0o777 == 0o600
        deadline = time.monotonic() + 1
        while not (tmp_path / "vm.log").read_text() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "secret" not in (tmp_path / "vm.log").read_text()
        assert "<redacted>" in (tmp_path / "vm.log").read_text()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_lume_share_arguments_are_read_only_by_default(tmp_path: Path) -> None:
    path = tmp_path / "shared"
    assert LumeDriver._share_argument(path, writable=False).endswith(":ro")
    assert LumeDriver._share_argument(path, writable=True).endswith(":rw")


def test_lume_driver_redacts_vnc_password_from_errors(tmp_path: Path) -> None:
    binary = tmp_path / "lume"
    binary.write_text("#!/bin/sh\necho 'failed vnc://:private@127.0.0.1:5901' >&2\nexit 1\n")
    binary.chmod(0o755)

    with pytest.raises(SmolVMError) as exc_info:
        LumeDriver(binary).inspect("mac-test", storage_path=tmp_path)

    assert "private" not in str(exc_info.value)
    assert "<redacted>" in str(exc_info.value)


def test_lume_driver_rejects_non_json_details(tmp_path: Path) -> None:
    binary = tmp_path / "lume"
    binary.write_text("#!/bin/sh\necho not-json\n")
    binary.chmod(0o755)

    with pytest.raises(SmolVMError, match="could not read"):
        LumeDriver(binary).inspect("mac-test", storage_path=tmp_path)


def test_open_desktop_uses_argument_vector_only() -> None:
    endpoint = DesktopEndpoint(port=5901)
    with (
        patch("smolvm.macos.desktop.platform.system", return_value="Darwin"),
        patch("smolvm.macos.desktop.subprocess.run") as run,
    ):
        run.return_value.returncode = 0
        open_desktop(endpoint)

    assert run.call_args.args[0] == ["open", "vnc://127.0.0.1:5901"]


def test_open_desktop_keeps_password_out_of_process_arguments() -> None:
    endpoint = DesktopEndpoint(port=5901)
    with (
        patch("smolvm.macos.desktop.platform.system", return_value="Darwin"),
        patch("smolvm.macos.desktop.subprocess.run") as run,
    ):
        run.return_value.returncode = 0
        open_desktop(endpoint, password="private secret")

    assert run.call_args.args[0] == ["osascript", "-"]
    assert "private%20secret" in run.call_args.kwargs["input"]
