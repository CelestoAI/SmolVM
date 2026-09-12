"""Black-box checks for the authenticated SDK-session supervisor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")


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
            f'{json.dumps({"protocol_version": 1, "token": token})}\n'.encode(),
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
        url = f'http://127.0.0.1:{ready["port"]}/sdk/v1/capabilities'

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
