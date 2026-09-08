"""Real SDK-process exit and facade reconnect on the disposable Linux host."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from smolvm import SmolVM
from smolvm.network_policy import NetworkPolicy
from smolvm.network_policy.placement import active_policy
from smolvm.types import VMConfig, VMInfo
from smolvm.vm import SmolVMManager

# This changes host networking and deliberately leaves evidence on failure.
# Run only on an expendable host whose entire disk can be deleted afterwards.
if os.environ.get("SMOLVM_POLICY_NATIVE_TESTS") != "1":
    raise SystemExit("Set SMOLVM_POLICY_NATIVE_TESTS=1 only on a disposable Linux host.")
if os.geteuid() != 0 or not all(Path(p).exists() for p in ("/dev/kvm", "/dev/vhost-vsock")):
    raise SystemExit("Native smoke requires root, KVM and vhost-vsock on Linux.")

ROOT = Path("/opt/policy-adoption")
ROOT.mkdir(exist_ok=True)
IMAGE = Path("/opt/policy-images/pi-vvalidation-20260630-amd64-qemu-alpine")


class ValidationManager(SmolVMManager):
    @staticmethod
    def _require_native_policy_lifecycle(config):
        pass  # Only the release gate; all runtime/enforcement code stays real.


if len(sys.argv) > 1:
    manager = ValidationManager(data_dir=ROOT, backend="qemu")
    manager.create(
        VMConfig(
            vm_id="native-adoption",
            backend="qemu",
            qemu_network="tap",
            comm_channel="vsock",
            kernel_path=IMAGE / "vmlinux.bin",
            rootfs_path=IMAGE / "rootfs.ext4",
            boot_args="console=ttyS0 reboot=k panic=1 init=/init",
            vcpu_count=1,
            memory=384,
            network_policy=NetworkPolicy(allowed_domains=["example.com"]),
        )
    )
    info = manager.start("native-adoption", boot_timeout=120)
    (ROOT / "vm.json").write_text(info.model_dump_json())
    print("creator exiting", flush=True)
    # Deliberately no stop or context-manager cleanup.
    sys.exit(0)

subprocess.run([sys.executable, __file__, "create"], check=True, timeout=180)
info = VMInfo.model_validate_json((ROOT / "vm.json").read_text())
time.sleep(4)
binding, state = active_policy(info, info.pid)
print(json.dumps({"event": "survived_creator_exit", "vm_pid": info.pid}), flush=True)
manager = SmolVMManager(data_dir=ROOT, backend="qemu")
manager.state.create_vm(info.config)
manager.state.update_vm(
    info.vm_id,
    status=info.status,
    network=info.network,
    pid=info.pid,
    control_socket_path=info.control_socket_path,
)
vm = SmolVM(vm_id=info.vm_id, state_manager=manager.state, data_dir=ROOT, backend="qemu")
result = vm.run("curl -fsS --max-time 15 https://example.com/")
assert result.exit_code == 0 and "Example Domain" in result.stdout, result
print(json.dumps({"event": "facade_reconnected_https"}), flush=True)
for who, pid in [
    ("vm", info.pid),
    ("worker", state["worker_pid"]),
    ("supervisor", state["supervisor_pid"]),
]:
    status = Path(f"/proc/{pid}/status").read_text().splitlines()
    print(
        json.dumps(
            {
                "event": "process_sample",
                "process": who,
                "status": [
                    line for line in status if line.startswith(("VmRSS:", "Threads:", "FDSize:"))
                ],
                "open_fds": len(list(Path(f"/proc/{pid}/fd").iterdir())),
            }
        ),
        flush=True,
    )
# Cached facade channels must reject changed host rules before dispatch.
binding.fence()
try:
    vm.run("echo must-not-dispatch")
except RuntimeError as error:
    assert "network protection could not be verified" in str(error), error
else:
    raise AssertionError("Cached facade dispatched after fencing")
print(json.dumps({"event": "cached_facade_rejected_fenced_policy"}), flush=True)
manager.stop(info.vm_id)
manager.delete(info.vm_id)
print(json.dumps({"event": "PASS"}), flush=True)
