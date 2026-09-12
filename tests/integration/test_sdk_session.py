"""Black-box checks for the authenticated SDK-session supervisor."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from smolvm.server.session import _read_handshake


@pytest.mark.parametrize("payload", [[], "token", 1, None])
def test_sdk_session_rejects_non_object_handshakes(payload: object) -> None:
    control = io.BytesIO(f"{json.dumps(payload)}\n".encode())

    with pytest.raises(ValueError, match="Invalid SDK control handshake"):
        _read_handshake(control)


def test_sdk_session_authenticates_and_exits_when_control_pipe_closes() -> None:
    read_fd, write_fd = os.pipe()
    command = [
        sys.executable,
        "-c",
        (
            "from smolvm.server.session import run_sdk_session; "
            f"raise SystemExit(run_sdk_session(control_fd={read_fd}))"
        ),
    ]
    process = subprocess.Popen(
        command,
        pass_fds=(read_fd,),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).parents[2],
    )
    os.close(read_fd)
    token = "a" * 43
    try:
        os.write(
            write_fd,
            f"{json.dumps({'protocol_version': 1, 'token': token})}\n".encode(),
        )
        assert process.stdout is not None
        readiness_line = process.stdout.readline()
        if not readiness_line:
            assert process.stderr is not None
            pytest.fail(process.stderr.read())
        ready = json.loads(readiness_line)
        assert ready == {
            "type": "smolvm.sdk.ready",
            "protocol_version": 1,
            "host": "127.0.0.1",
            "port": ready["port"],
        }
        url = f"http://127.0.0.1:{ready['port']}/sdk/v1/capabilities"

        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(url, timeout=5)
        assert unauthorized.value.code == 401

        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        assert payload["protocol_version"] == 1
        assert "sandbox.create" in payload["capabilities"]
    finally:
        os.close(write_fd)

    assert process.wait(timeout=10) == 0


def test_sdk_session_exits_after_hard_parent_termination() -> None:
    parent_script = """
import json
import os
import subprocess
import sys
import time

read_fd, write_fd = os.pipe()
child = subprocess.Popen(
    [sys.executable, "-c", (
        "from smolvm.server.session import run_sdk_session; "
        f"raise SystemExit(run_sdk_session(control_fd={read_fd}))"
    )],
    pass_fds=(read_fd,),
    stdout=subprocess.PIPE,
    text=True,
)
os.close(read_fd)
os.write(write_fd, (json.dumps({"protocol_version": 1, "token": "a" * 43}) + "\\n").encode())
ready = json.loads(child.stdout.readline())
print(json.dumps({"server_pid": child.pid, "ready": ready}), flush=True)
time.sleep(60)
"""
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).parents[2],
    )
    server_pid: int | None = None
    try:
        assert parent.stdout is not None
        line = parent.stdout.readline()
        if not line:
            assert parent.stderr is not None
            pytest.fail(parent.stderr.read())
        record = json.loads(line)
        server_pid = record["server_pid"]
        ready = record["ready"]
        url = f"http://127.0.0.1:{ready['port']}/sdk/v1/capabilities"

        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(timeout=5)

        deadline = time.monotonic() + 10
        while True:
            try:
                urllib.request.urlopen(url, timeout=0.25)
            except urllib.error.HTTPError:
                pass
            except (urllib.error.URLError, OSError):
                break
            if time.monotonic() >= deadline:
                pytest.fail("SDK session remained reachable after its parent was killed")
            time.sleep(0.05)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if server_pid is not None:
            with suppress(ProcessLookupError):
                os.kill(server_pid, signal.SIGTERM)
