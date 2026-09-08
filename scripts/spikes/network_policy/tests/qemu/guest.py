"""QEMU/serial fixture using SHA-pinned published SmolVM Ubuntu images."""

import base64
import json
import select
import shlex
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path


class Guest:
    def __init__(self, directory: Path, tap: str, address: str, gateway: str, *, restore_disk=None):
        directory.mkdir()
        self.directory = directory
        self.console = None
        self.process = None
        self.buffer = b""
        self.disk = directory / "disk.qcow2"
        if restore_disk is None:
            subprocess.run(
                [
                    "qemu-img",
                    "create",
                    "-f",
                    "qcow2",
                    "-F",
                    "raw",
                    "-b",
                    "/images/rootfs.ext4",
                    str(self.disk),
                ],
                check=True,
                capture_output=True,
            )
        else:
            shutil.copyfile(restore_disk, self.disk)
        serial = directory / "serial.sock"
        self.log = (directory / "qemu.log").open("wb")
        self.process = subprocess.Popen(
            [
                "qemu-system-aarch64",
                "-machine",
                "virt",
                "-accel",
                "tcg",
                "-cpu",
                "max",
                "-m",
                "512",
                "-smp",
                "1",
                "-nodefaults",
                "-display",
                "none",
                "-no-reboot",
                "-kernel",
                "/images/vmlinux.bin",
                "-append",
                "console=ttyAMA0 root=/dev/vda rw init=/bin/bash quiet random.trust_cpu=on",
                "-drive",
                f"file={self.disk},if=none,id=disk,format=qcow2",
                "-device",
                "virtio-blk-pci,drive=disk",
                "-netdev",
                f"tap,id=net,ifname={tap},script=no,downscript=no",
                "-device",
                "virtio-net-pci,netdev=net,romfile=",
                "-chardev",
                f"socket,id=console,path={serial},server=on,wait=on",
                "-serial",
                "chardev:console",
                "-monitor",
                "none",
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.log,
            stderr=self.log,
        )
        try:
            deadline = time.monotonic() + 30
            while not serial.exists():
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("QEMU did not create its serial socket")
                time.sleep(0.02)
            self.console = socket.socket(socket.AF_UNIX)
            self.console.connect(str(serial))
            self._until(b":/# ", 90)
            # Real root guest. No guest agent or production init-path claims.
            self.command(
                "mount -t proc proc /proc; mount -t sysfs sys /sys; "
                "mount -t devtmpfs dev /dev; "
                f"ip link set lo up; ip link set eth0 up; ip addr add {address} dev eth0; "
                f"ip route add default via {gateway}; stty -echo; "
                "hostname fixture; printf '127.0.0.1 localhost fixture\\n' >/etc/hosts; "
                "printf 'nameserver 127.0.0.1\\noptions timeout:1 attempts:1\\n' "
                ">/etc/resolv.conf; "
                "python3 --version; curl --version | head -1",
                timeout=30,
            )
        except BaseException:
            self.close()
            raise

    def _until(self, marker: bytes, timeout: float):
        deadline = time.monotonic() + timeout
        while marker not in self.buffer:
            if len(self.buffer) > 1024 * 1024:
                raise RuntimeError("guest console output exceeded fixture limit")
            if not select.select([self.console], [], [], max(0, deadline - time.monotonic()))[0]:
                raise TimeoutError(f"guest marker not received; tail={self.buffer[-1000:]!r}")
            data = self.console.recv(65536)
            if not data:
                raise RuntimeError(
                    "guest console closed: "
                    + (self.directory / "qemu.log").read_text(errors="replace")[-2000:]
                    + repr(self.buffer[-2000:])
                )
            self.buffer += data
        before, self.buffer = self.buffer.split(marker, 1)
        return before

    def command(self, command: str, timeout: float = 15):
        marker = f"DONE_{uuid.uuid4().hex}"
        # Split the marker so terminal echo cannot accidentally satisfy it.
        wrapped = command + f"; printf '\\n%s%s\\n' '{marker[:10]}' '{marker[10:]}'\n"
        self.console.sendall(wrapped.encode())
        return self._until(marker.encode(), timeout).decode(errors="replace")

    def python(self, script: str, timeout: float = 15):
        encoded = base64.b64encode(script.encode()).decode()
        command = "python3 -c " + shlex.quote(f"import base64; exec(base64.b64decode('{encoded}'))")
        return self.command(command, timeout)

    def write(self, path: str, content: bytes):
        self.python(f"open({path!r}, 'wb').close()")
        # Stay below the serial terminal's canonical input-line limit.
        for offset in range(0, len(content), 512):
            encoded = base64.b64encode(content[offset : offset + 512]).decode()
            self.python(f"import base64; open({path!r}, 'ab').write(base64.b64decode({encoded!r}))")

    def json(self, script: str, timeout: float = 15):
        output = self.python(script, timeout)
        line = next(line for line in output.splitlines() if line.startswith("RESULT="))
        return json.loads(line.removeprefix("RESULT="))

    def close(self):
        if self.console is not None:
            self.console.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.log.close()
