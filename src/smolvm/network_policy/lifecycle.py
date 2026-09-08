"""Persistent per-VM proxy supervision, independent of the SDK caller's lifetime.

Prepare before boot, attach the actual VM process after launch, and configure
public trust before exposing workload APIs. The VM owner removes firewall rules
only after this supervisor and its VM have stopped.
"""

import fcntl
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

from . import NetworkPolicy
from .firewall import NetworkBinding
from .process import ManagedProxy
from .setup import require_runtime

_MAX_MESSAGE = 65536


def process_identity(pid: int) -> str | None:
    """Linux process birth identity; zombies are not live owners."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + stat[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def _receive(pipe: BinaryIO, timeout: float) -> dict:
    data = bytearray()
    deadline = time.monotonic() + timeout
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([pipe], [], [], remaining)[0]:
            raise TimeoutError("Network policy supervision timed out.")
        chunk = os.read(pipe.fileno(), 4096)
        if not chunk:
            raise RuntimeError("Network policy supervisor disconnected.")
        data.extend(chunk)
        if len(data) >= _MAX_MESSAGE:
            raise RuntimeError("Network policy control message is too large.")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise RuntimeError("Invalid network policy control message.")
    return value


def _send(pipe: BinaryIO, value: dict) -> None:
    message = json.dumps(value, sort_keys=True).encode() + b"\n"
    if len(message) >= _MAX_MESSAGE:
        raise ValueError("Network policy control message is too large.")
    pipe.write(message)
    pipe.flush()


def _write_state(path: Path, state: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".policy-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def binding_identity(binding: NetworkBinding) -> str:
    return hashlib.sha256(json.dumps(asdict(binding), sort_keys=True).encode()).hexdigest()


class PreparedPolicy:
    """A pre-boot supervisor reservation; attach or abort exactly once."""

    def __init__(
        self,
        policy: NetworkPolicy,
        binding: NetworkBinding,
        state_path: Path,
        *,
        startup_timeout: float = 60,
    ):
        require_runtime(policy)
        if bool(policy.allowed_domains) != (binding.proxy_uid is not None):
            raise ValueError("Network policy and proxy placement do not match.")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("Strict networking requires Linux process-handle support.")
        self.binding = binding
        self.state_path = state_path
        self.process = None
        self.attached = False
        self.public_ca = b""
        self.nonce = uuid.uuid4().hex
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(
            state_path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "This sandbox already has a network policy supervisor."
                ) from error
            # An unlocked file can still describe an orphan VM or worker.
            # Only verified stopped-runtime cleanup releases that placement.
            if state_path.exists():
                raise RuntimeError(
                    "Previous network policy ownership needs stopped-runtime cleanup."
                )
            _require_unused_uid(binding)
            # Persist ownership before any kernel mutation or child launch. An
            # interrupted preparation must not look like an unused placement.
            _write_state(
                state_path,
                {
                    "status": "reserved",
                    "nonce": self.nonce,
                    "policy_identity": policy.identity,
                    "binding_identity": binding_identity(binding),
                    "binding": asdict(binding),
                },
            )
            binding.install()
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("supervisor.py"))],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
                pass_fds=(lock,),
                env={
                    "LANG": "C.UTF-8",
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
            )
            _send(
                self.process.stdin,
                {
                    "policy": policy.model_dump(mode="json"),
                    "binding": asdict(binding),
                    "state_path": str(state_path.resolve()),
                    "nonce": self.nonce,
                    "startup_timeout": startup_timeout,
                },
            )
            ready = _receive(self.process.stdout, 20)
            if (
                ready.get("nonce") != self.nonce
                or ready.get("supervisor_pid") != self.process.pid
                or ready.get("policy_identity") != policy.identity
                or ready.get("binding_identity") != binding_identity(binding)
                or ready.get("status") != "prepared"
            ):
                raise RuntimeError("Network policy supervisor identity did not match.")
            self.public_ca = ready["public_ca"].encode("ascii")
        except BaseException:
            if self.process is not None:
                self.abort()
            raise
        finally:
            # Do not LOCK_UN: the child holds the same open-file description.
            os.close(lock)

    def attach(self, vm_pid: int) -> None:
        if self.attached or self.process is None:
            raise RuntimeError("Network policy reservation is not attachable.")
        identity = process_identity(vm_pid)
        if identity is None:
            raise RuntimeError("The sandbox process exited before policy attachment.")
        _send(self.process.stdin, {"vm_pid": vm_pid, "vm_identity": identity, "nonce": self.nonce})
        reply = _receive(self.process.stdout, 5)
        if reply.get("status") != "active" or reply.get("nonce") != self.nonce:
            raise RuntimeError("Network policy attachment was not acknowledged.")
        self.attached = True
        self.process.stdin.close()
        self.process.stdout.close()

    def abort(self) -> None:
        """Before attachment: fence even if terminating the supervisor fails."""
        if self.attached:
            raise RuntimeError("Stop the VM before releasing its active network policy.")
        try:
            self.binding.fence()
        finally:
            if self.process is not None:
                if not self.process.stdin.closed:
                    with suppress(BrokenPipeError):
                        self.process.stdin.close()
                self.process.wait(timeout=20)
                self.process.stdout.close()


def policy_status(
    state_path: Path, policy: NetworkPolicy, binding: NetworkBinding, vm_pid: int
) -> dict | None:
    """Do not adopt based on guest health or a stale readiness file."""
    try:
        state = json.loads(state_path.read_text())
        if (
            not state.get("vm_identity")
            or not state.get("supervisor_identity")
            or (policy.allowed_domains and not state.get("worker_identity"))
            or state.get("status") != "active"
            or state.get("policy_identity") != policy.identity
            or state.get("binding_identity") != binding_identity(binding)
            or state.get("vm_pid") != vm_pid
            or state.get("vm_identity") != process_identity(vm_pid)
            or state.get("supervisor_identity") != process_identity(state["supervisor_pid"])
            or (
                policy.allowed_domains
                and state.get("worker_identity") != process_identity(state["worker_pid"])
            )
            or state.get("firewall_identity") != binding.fingerprint()
        ):
            return None
        return state
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def supervise() -> None:
    """Internal daemon: own the worker and a stable kernel handle to its VM."""
    config = _receive(sys.stdin.buffer, 10)
    policy = NetworkPolicy.model_validate(config["policy"])
    binding = NetworkBinding(**config["binding"])
    path = Path(config["state_path"])
    state = {
        "status": "starting",
        "nonce": config["nonce"],
        "supervisor_pid": os.getpid(),
        "supervisor_identity": process_identity(os.getpid()),
        "policy_identity": policy.identity,
        "binding_identity": binding_identity(binding),
    }
    worker = None
    vm_handle = None
    stop_requested = False

    def stop(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        state.update(binding=asdict(binding), public_ca="")
        if policy.allowed_domains:
            worker = ManagedProxy(sys.executable, policy=policy, binding=binding)
            state["worker_directory"] = str(worker.directory)
            _write_state(path, state)
            worker.start()
            state.update(
                public_ca=worker.public_ca.decode("ascii"),
                worker_pid=worker.process.pid,
                worker_identity=process_identity(worker.process.pid),
                firewall_identity=worker.firewall_identity,
            )
        else:
            state["firewall_identity"] = binding.fingerprint()
        state["status"] = "prepared"
        _write_state(path, state)
        _send(sys.stdout.buffer, state)
        attachment = _receive(sys.stdin.buffer, config["startup_timeout"])
        if attachment.get("nonce") != config["nonce"]:
            raise RuntimeError("Invalid policy attachment.")
        vm_pid = attachment["vm_pid"]
        if type(vm_pid) is not int or vm_pid <= 1 or vm_pid == os.getpid():
            raise RuntimeError("Invalid sandbox process.")
        vm_handle = os.pidfd_open(vm_pid)
        if process_identity(vm_pid) != attachment["vm_identity"]:
            os.close(vm_handle)
            vm_handle = None
            raise RuntimeError("Sandbox process identity changed.")
        if worker is not None and (worker.failure or worker.process.poll() is not None):
            raise RuntimeError("Network policy failed before sandbox attachment.")
        if binding.fingerprint() != state["firewall_identity"]:
            raise RuntimeError("Network policy rules changed before sandbox attachment.")
        state.update(status="active", vm_pid=vm_pid, vm_identity=attachment["vm_identity"])
        _write_state(path, state)
        _send(sys.stdout.buffer, {"status": "active", "nonce": config["nonce"]})
        # The caller may now exit. Only VM/worker lifetime controls this daemon.
        while not select.select([vm_handle], [], [], 0.25)[0]:
            if (
                stop_requested
                or (worker is not None and (worker.process.poll() is not None or worker.failure))
                or (worker is None and binding.fingerprint() != state["firewall_identity"])
            ):
                state["status"] = "failed"
                # Kill through the validated kernel handle, never a recycled PID.
                signal.pidfd_send_signal(vm_handle, signal.SIGKILL)
                break
        else:
            state["status"] = "stopped"
    except BaseException:
        state["status"] = "failed"
        if vm_handle is not None:
            with suppress(ProcessLookupError):
                signal.pidfd_send_signal(vm_handle, signal.SIGKILL)
        raise
    finally:
        try:
            if worker is not None:
                worker.close()
            else:
                binding.fence()
        except Exception:
            state["status"] = "failed"
            if vm_handle is not None:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(vm_handle, signal.SIGKILL)
            raise
        finally:
            if vm_handle is not None:
                os.close(vm_handle)
            _write_state(path, state)


def _require_unused_uid(binding: NetworkBinding) -> None:
    """Check under the placement lock, including unrecorded orphan workers."""
    if binding.proxy_uid is not None:
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal():
                continue
            try:
                fields = dict(
                    line.split(":", 1) for line in (process / "status").read_text().splitlines()
                )
                if (
                    str(binding.proxy_uid) in fields["Uid"].split()
                    and process_identity(int(process.name)) is not None
                ):
                    raise RuntimeError("The sandbox's network identity is still in use.")
            except (FileNotFoundError, ProcessLookupError):
                continue


def cleanup_stopped_policy(state_path: Path, binding: NetworkBinding) -> None:
    """Release policy resources AFTER the runtime owner has verified VM shutdown.

    Keep the VM's IP/UID reservation until this succeeds. Incomplete launch state
    is not itself proof that no VM exists; the backend must verify shutdown.
    """
    import shutil
    import stat

    from .firewall import _nft

    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(state_path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("The sandbox network supervisor has not stopped yet.") from error
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if state and state.get("binding_identity") != binding_identity(binding):
            raise RuntimeError("Cannot release another sandbox's network policy.")
        for component in ("vm", "worker", "supervisor"):
            identity = state.get(f"{component}_identity")
            if identity and process_identity(state.get(f"{component}_pid")) == identity:
                raise RuntimeError("The sandbox or its network worker is still running.")
        _require_unused_uid(binding)
        directory = state.get("worker_directory")
        if directory:
            private = Path(directory)
            if not private.is_absolute() or not private.name.startswith("smolvm-policy-"):
                raise RuntimeError("Invalid network worker directory.")
            try:
                metadata = private.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != binding.proxy_uid:
                    raise RuntimeError("Network worker directory ownership changed.")
                shutil.rmtree(private)
        tables = json.loads(_nft("-j", "list", "tables"))["nftables"]
        if any(
            item.get("table", {}).get("family") == "inet"
            and item.get("table", {}).get("name") == binding.table
            for item in tables
        ):
            binding.remove()
        state_path.unlink(missing_ok=True)
        # Keep the lock inode: unlinking it while locked allows two owners.
    finally:
        os.close(lock)
