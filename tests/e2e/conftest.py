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

"""Shared fixtures for the real-KVM end-to-end suite.

These tests boot an actual QEMU micro-VM through the public ``SmolVM`` API
(unlike the unit suite, which mocks the hypervisor). They run nightly on a
GitHub ``ubuntu-latest`` runner, which exposes ``/dev/kvm``.

The ``vm`` fixture is parametrized over both control transports — SSH and
vsock — so the whole lifecycle (``test_lifecycle.py``) is exercised once per
transport against a single, shared sandbox.
"""

from __future__ import annotations

from contextlib import suppress

import pytest
from _util import BOOT_TIMEOUT, kvm_ready

from smolvm import SmolVM
from smolvm.comm import host_supports_vsock
from smolvm.types import VMState


@pytest.fixture(scope="module", params=["ssh", "vsock"])
def vm(request: pytest.FixtureRequest):
    """A single running QEMU sandbox, shared across the lifecycle tests.

    Yields one started ``SmolVM`` per transport. Teardown is best-effort:
    ``test_stop_and_cleanup`` deletes the sandbox as its final assertion, so
    the ``stop``/``delete`` here are just a safety net for earlier failures.
    """
    transport = request.param

    if not kvm_ready():
        pytest.skip(
            "requires /dev/kvm and a working smolvm-core native extension "
            "(enable KVM with `sudo modprobe kvm && sudo chmod 666 /dev/kvm`, "
            "then re-run)"
        )
    if transport == "vsock" and not host_supports_vsock():
        pytest.skip(
            "vsock requires a Linux host with /dev/vhost-vsock "
            "(load it via `sudo modprobe vhost_vsock`)"
        )

    # Pin to Alpine: the SmolVM-*built* image (vsock guest agent + python3 baked
    # in by ImageBuilder, SSH key injected on the kernel cmdline, no cloud-init
    # seed ISO). The QEMU default is Ubuntu, whose cloud qcow2 carries a seed
    # ISO as an extra drive (snapshot then rejects it) and lacks the baked-in
    # agent (vsock never answers) — neither of which is what we want to smoke.
    sandbox = SmolVM(backend="qemu", os="alpine", comm_channel=transport)
    try:
        sandbox.start(boot_timeout=BOOT_TIMEOUT)
        assert sandbox.status == VMState.RUNNING
        yield sandbox
    finally:
        # Best-effort: test_stop_and_cleanup may already have stopped/deleted it.
        with suppress(Exception):
            sandbox.stop()
        with suppress(Exception):
            sandbox.delete()
