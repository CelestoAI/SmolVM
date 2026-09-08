"""Own one isolated proxy worker while its supervising process is alive.

The VM lifecycle supervisor owns this object, not a short-lived CLI invocation.
Firewall installation precedes construction; failed fencing requires VM stop.
"""

import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path

from . import NetworkPolicy
from .firewall import NetworkBinding
from .setup import require_runtime


class ManagedProxy:
    def __init__(
        self,
        python: str,
        *,
        policy: NetworkPolicy,
        binding: NetworkBinding,
        upstream_ca: str | None = None,
    ):
        if not policy.allowed_domains or binding.proxy_uid is None:
            raise ValueError("Only a nonempty policy needs a proxy worker.")
        require_runtime(policy)
        uid = binding.proxy_uid
        self.binding = binding
        self.process = None
        self.monitor = None
        self.lock = threading.RLock()
        # Serialize start/close without blocking the watcher's firewall lock
        # while close joins that watcher.
        self.lifecycle_lock = threading.Lock()
        self.closed = False
        self.fenced = True
        self.failure = None
        self.public_ca = None
        self.firewall_identity = None
        self.python = python
        self.directory = Path(tempfile.mkdtemp(prefix="smolvm-policy-"))
        try:
            os.chown(self.directory, uid, uid)
        except BaseException:
            self.directory.rmdir()
            raise
        self.uid = uid
        self.config = {
            "allowed_domains": list(policy.allowed_domains),
            "confdir": str(self.directory),
            "gateway": binding.gateway_ip,
            "port": binding.proxy_port,
            "host_addresses": sorted(binding.host_addresses),
            "upstream_ca": upstream_ca,
        }

    def fence(self):
        with self.lock:
            self.fenced = True
            self.binding.fence()

    def start(self):
        with self.lifecycle_lock:
            if self.closed or self.process is not None:
                raise RuntimeError("worker is already allocated")
            self.fence()
            # setpriv drops privileges before Python imports or parses config.
            # No preexec_fn (unsafe in a multithreaded host agent), inherited
            # credential environment, supplementary groups or extra FDs.
            self.process = subprocess.Popen(
                [
                    "/usr/bin/setpriv",
                    f"--reuid={self.uid}",
                    f"--regid={self.uid}",
                    "--clear-groups",
                    "--inh-caps=-all",
                    "--ambient-caps=-all",
                    "--bounding-set=-all",
                    "--no-new-privs",
                    self.python,
                    str(Path(__file__).with_name("worker.py")),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env={"LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            try:
                encoded = json.dumps(self.config, sort_keys=True).encode()
                self.process.stdin.write(encoded + b"\n")
                self.process.stdin.flush()
                data = b""
                deadline = time.monotonic() + 15
                while b"\n" not in data:
                    if (
                        len(data) > 8192
                        or not select.select(
                            [self.process.stdout], [], [], max(0, deadline - time.monotonic())
                        )[0]
                    ):
                        raise RuntimeError("worker readiness failed")
                    chunk = os.read(self.process.stdout.fileno(), 8192)
                    if not chunk:
                        raise RuntimeError("worker exited before readiness")
                    data += chunk
                ready = json.loads(data)
                if (
                    ready["identity"] != hashlib.sha256(encoded).hexdigest()
                    or ready["pid"] != self.process.pid
                    or ready["status"] != "ready"
                ):
                    raise RuntimeError("worker identity mismatch")
                status = dict(
                    line.split(":", 1)
                    for line in Path(f"/proc/{self.process.pid}/status").read_text().splitlines()
                )
                if (
                    status["Uid"].split() != [str(self.uid)] * 4
                    or status["Gid"].split() != [str(self.uid)] * 4
                    or status["Groups"].strip()
                    or status["NoNewPrivs"].strip() != "1"
                    or any(
                        int(status[name], 16)
                        for name in ("CapInh", "CapPrm", "CapEff", "CapAmb", "CapBnd")
                    )
                ):
                    raise RuntimeError("worker privileges mismatch")
                self.public_ca = ready["ca_cert"].encode()
                # Negative readiness check before guest admission, not an HTTP
                # 200/liveness test that could pass with a missing policy.
                with socket.create_connection(
                    (self.config["gateway"], self.config["port"]), 3
                ) as client:
                    denied_name = f"probe-{uuid.uuid4().hex}.invalid"
                    if denied_name in self.config["allowed_domains"]:
                        raise RuntimeError("readiness probe must not be allowed")
                    client.sendall(
                        f"GET http://{denied_name}/ HTTP/1.1\r\nHost: {denied_name}\r\n"
                        "Connection: close\r\n\r\n".encode()
                    )
                    if client.recv(1):
                        raise RuntimeError("worker negative readiness failed")
                if self.process.poll() is not None:
                    raise RuntimeError("worker exited during readiness")
                with self.lock:
                    self.binding.admit()
                    self.firewall_identity = self.binding.fingerprint()
                    self.fenced = False
                self.monitor = threading.Thread(target=self._watch, daemon=True)
                self.monitor.start()
            except BaseException:
                try:
                    self.fence()
                finally:
                    # A fencing error must not leave a partially started worker
                    # running. The caller must still quarantine on the error.
                    self.process.kill()
                    self.process.wait(timeout=5)
                raise

    def _watch(self):
        try:
            while True:
                try:
                    self.process.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    with self.lock:
                        if self.fenced:
                            return
                        if self.binding.fingerprint() != self.firewall_identity:
                            raise RuntimeError("Network policy rules changed.") from None
                        self.binding.renew_admission()
        except Exception:
            self.failure = "firewall_refresh_failed"
        finally:
            try:
                self.fence()
            except Exception:
                # The persistent lifecycle owner quarantines the VM. The
                # kernel lease expires even if this owner itself disappears.
                self.failure = "firewall_fence_failed"

    def close(self):
        with self.lifecycle_lock:
            if self.closed:
                return
            fence_error = None
            try:
                self.fence()
            except Exception as error:
                fence_error = error
                self.failure = "firewall_fence_failed"
            if self.process is not None:
                if self.process.stdin is not None and not self.process.stdin.closed:
                    if fence_error is None:
                        # Successful fencing permits immediate release of
                        # the listening port; EOF alone requires a grace.
                        with suppress(BrokenPipeError):
                            self.process.stdin.write(b"S")
                            self.process.stdin.flush()
                    with suppress(BrokenPipeError):
                        self.process.stdin.close()
                try:
                    self.process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
                if self.monitor is not None:
                    self.monitor.join(timeout=5)
                    if self.monitor.is_alive():
                        raise RuntimeError("worker cleanup did not verify fencing")
                self.process.stdout.close()
            with suppress(FileNotFoundError):
                shutil.rmtree(self.directory)
            if fence_error or self.failure:
                raise RuntimeError("worker cleanup did not verify fencing") from fence_error
            self.closed = True
