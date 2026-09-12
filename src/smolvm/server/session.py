# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Supervise one authenticated SDK bridge over an inherited control pipe."""

from __future__ import annotations

import json
import os
import socket
import threading
from typing import BinaryIO

from smolvm.server.app import create_app


def _read_handshake(control: BinaryIO) -> str:
    record = json.loads(control.readline(4097))
    if not isinstance(record, dict):
        raise ValueError("Invalid SDK control handshake.")
    token = record.get("token")
    if record.get("protocol_version") != 1 or not isinstance(token, str) or len(token) < 43:
        raise ValueError("Invalid SDK control handshake.")
    return token


def run_sdk_session(*, control_fd: int = 3) -> int:
    """Run until the parent closes ``control_fd``, then clean up the session."""
    import uvicorn

    control = os.fdopen(control_fd, "rb", buffering=0, closefd=False)
    token = _read_handshake(control)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]

    config = uvicorn.Config(
        create_app(auth_token=token),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def watch_parent() -> None:
        try:
            control.read()
        finally:
            server.should_exit = True

    threading.Thread(target=watch_parent, name="smolvm-sdk-parent", daemon=True).start()
    print(
        json.dumps(
            {
                "type": "smolvm.sdk.ready",
                "protocol_version": 1,
                "host": "127.0.0.1",
                "port": port,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.run(sockets=[listener])
    finally:
        listener.close()
    return 0
