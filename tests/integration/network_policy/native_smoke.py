"""Disposable-host native smoke; bypass only the not-yet-released API gate."""

import json
import os
import signal
import time
from pathlib import Path

from smolvm.comm.rust_http_vsock_channel import RustHttpVsockChannel
from smolvm.images.published import ensure_published_image, lookup
from smolvm.network_policy import NetworkPolicy
from smolvm.network_policy.lifecycle import process_identity
from smolvm.network_policy.placement import active_policy
from smolvm.runtime.boot_profiles import KernelBootProfile, get_boot_profile_spec
from smolvm.types import VMConfig
from smolvm.vm import SmolVMManager

# This changes host networking and deliberately leaves evidence on failure.
# Run only on an expendable host whose entire disk can be deleted afterwards.
if os.environ.get("SMOLVM_POLICY_NATIVE_TESTS") != "1":
    raise SystemExit("Set SMOLVM_POLICY_NATIVE_TESTS=1 only on a disposable Linux host.")
if os.geteuid() != 0 or not all(Path(p).exists() for p in ("/dev/kvm", "/dev/vhost-vsock")):
    raise SystemExit("Native smoke requires root, KVM and vhost-vsock on Linux.")


class ValidationManager(SmolVMManager):
    @staticmethod
    def _require_native_policy_lifecycle(config):
        # Deliberately bypass the release gate in this test only. All enforcement,
        # QEMU argv, launch, real init/vsock, trust and cleanup are production code.
        pass


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


entry = lookup("pi", "amd64", "qemu", "alpine")
entry = entry.model_copy(
    update={
        "kernel_url": "https://github.com/CelestoAI/SmolVM/releases/download/images-2026.06.30.0/vmlinux-amd64.image",
        "rootfs_url": "https://github.com/CelestoAI/SmolVM/releases/download/images-2026.06.30.0/pi-amd64-alpine-rootfs.ext4.zst",
        "kernel_sha256": "a7a8da6ad55236edccbdd1015eda023d68a4879be648a348462119138de54cb3",
        "rootfs_sha256": "663411193e9792f3cb5fce88a1b55f7ca12064021d58b4d5b0ed274d2169c460",
    }
)
image = ensure_published_image(
    "pi",
    "amd64",
    "qemu",
    "alpine",
    cache_dir=Path("/opt/policy-images"),
    manifest={("pi", "amd64", "qemu", "alpine"): entry},
    version="validation-20260630",
)
emit("image_ready", kernel=str(image.kernel_path), rootfs=str(image.rootfs_path))
manager = ValidationManager(data_dir=Path("/opt/policy-native-state"), backend="qemu")
for suffix, domains in [("deny", []), ("allow", ["example.com"])]:
    name = "native-policy-" + suffix
    info = manager.create(
        VMConfig(
            vm_id=name,
            vcpu_count=1,
            memory=384,
            backend="qemu",
            qemu_network="tap",
            comm_channel="vsock",
            kernel_path=image.kernel_path,
            rootfs_path=image.rootfs_path,
            boot_args=get_boot_profile_spec(
                KernelBootProfile.MICROVM_DIRECT
            ).base_boot_args_for_backend("qemu", "x86_64"),
            network_policy=NetworkPolicy(allowed_domains=domains),
        )
    )
    emit("created", vm=name)
    channel = None
    try:
        for attempt in range(2):
            started = time.monotonic()
            info = manager.start(name, boot_timeout=120)
            binding, state = active_policy(info, info.pid)
            channel = RustHttpVsockChannel.from_cid(info.config.vsock.guest_cid, sandbox_name=name)
            channel.wait_ready(timeout=120)
            result = channel.run("echo native-ready", timeout=10)
            assert result.exit_code == 0 and "native-ready" in result.stdout, result
            emit("native_ready", vm=name, attempt=attempt, seconds=time.monotonic() - started)
            for cmd in [
                "curl -fsS --max-time 3 --noproxy '*' https://1.1.1.1/",
                "curl -fsS --max-time 3 --noproxy '*' http://169.254.169.254/computeMetadata/v1/",
            ]:
                result = channel.run(cmd, timeout=10)
                assert result.exit_code != 0, result
            if domains:
                result = channel.run("curl -fsS --max-time 15 https://example.com/", timeout=20)
                assert result.exit_code == 0 and "Example Domain" in result.stdout, result
                result = channel.run("curl -fsS --max-time 5 https://example.net/", timeout=10)
                assert result.exit_code != 0, result
            else:
                assert state.get("worker_pid") is None, state
            emit("egress_checked", vm=name, attempt=attempt)
            manager.pause(name)
            manager.resume(name)
            manager.ensure_network_connectivity(manager.get(name))
            channel.close()
            channel = None
            if domains and attempt == 1:
                os.kill(state["worker_pid"], signal.SIGKILL)
                deadline = time.monotonic() + 10
                while process_identity(info.pid) is not None and time.monotonic() < deadline:
                    time.sleep(0.1)
                assert process_identity(info.pid) is None
                emit("worker_failure_killed_vm", vm=name)
            manager.stop(name)
            _, path = (binding, Path("/run/smolvm/network-policy") / f"{binding.tap}.json")
            assert not path.exists()
            emit("stopped_clean", vm=name, attempt=attempt)
        manager.delete(name)
        emit("deleted", vm=name)
    except BaseException:
        # Preserve evidence on failure; the entire cloud VM is disposable.
        emit("failed", vm=name, info=manager.get(name).model_dump(mode="json"))
        raise
    finally:
        if channel is not None:
            channel.close()
emit("PASS")
