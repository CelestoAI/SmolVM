#!/usr/bin/env python3

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

"""Start the fake legacy portal from inside the SmolVM sandbox."""

from __future__ import annotations

import argparse
import socket
import subprocess
import time
from pathlib import Path


def port_is_ready(host: str, port: int) -> bool:
    """Return True when a local TCP port accepts connections."""
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        return False
    return True


def wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    """Wait until the portal is accepting connections."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_is_ready(host, port):
            return
        time.sleep(0.25)
    raise RuntimeError(f"Acme portal did not start on {host}:{port}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the Acme legacy reports portal.")
    parser.add_argument("--root", required=True, help="Mounted demo root inside the sandbox.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if port_is_ready(args.host, args.port):
        print(f"Acme portal already ready at http://{args.host}:{args.port}")
        return 0

    root = Path(args.root)
    portal_dir = root / "portal"
    with open("/tmp/acme-portal.log", "a", encoding="utf-8") as log:
        subprocess.Popen(
            [
                "python3",
                str(portal_dir / "server.py"),
                "--host",
                args.host,
                "--port",
                str(args.port),
            ],
            cwd=portal_dir,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    wait_for_port(args.host, args.port)
    print(f"Acme portal ready at http://{args.host}:{args.port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
