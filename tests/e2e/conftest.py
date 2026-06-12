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

These tests boot actual micro-VMs through the public ``SmolVM`` API (unlike
the unit suite, which mocks the hypervisor). They run in CI on GitHub
``ubuntu-latest`` runners, which expose ``/dev/kvm``.

The ``vm`` fixture is parametrized over supported backend/transport variants
so the whole lifecycle (``test_lifecycle.py``) is exercised against a single,
shared sandbox for each variant.
"""

from __future__ import annotations

import os
from contextlib import suppress

import pytest
from _util import (
    BOOT_TIMEOUT,
    E2E_BACKENDS,
    E2E_VARIANTS,
    E2EVariant,
    require_backend_available,
    selected_backend,
)

from smolvm import SmolVM
from smolvm.comm import host_supports_vsock
from smolvm.runtime.backends import BACKEND_QEMU
from smolvm.types import VMState


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add a backend selector so CI can run one backend per matrix job."""
    parser.addoption(
        "--e2e-backend",
        choices=("all", *E2E_BACKENDS),
        default=os.environ.get("SMOLVM_E2E_BACKEND", "all"),
        help="Backend to exercise in tests/e2e (default: all available backends).",
    )

@pytest.fixture(scope="module", params=E2E_VARIANTS, ids=lambda variant: variant.id)
def e2e_variant(request: pytest.FixtureRequest) -> E2EVariant:
    """Selected backend/transport variant for this module fixture instance."""
    variant = request.param
    selected = selected_backend(request.config)
    if selected != "all" and variant.backend != selected:
        pytest.skip(
            f"End-to-end tests for '{variant.backend}' are skipped because this run "
            f"selected '{selected}'; rerun all backends with: pytest tests/e2e."
        )
    require_backend_available(variant.backend, request.config, sandbox_name=variant.id)
    return variant


@pytest.fixture(scope="module")
def vm(e2e_variant: E2EVariant):
    """A single running sandbox, shared across the lifecycle tests.

    Yields one started ``SmolVM`` per backend/transport variant. Teardown is
    best-effort: ``test_stop_and_cleanup`` deletes the sandbox as its final
    assertion, so the ``stop``/``delete`` here are just a safety net for
    earlier failures.
    """
    if (
        e2e_variant.transport == "vsock"
        and e2e_variant.backend == BACKEND_QEMU
        and not host_supports_vsock()
    ):
        pytest.skip(
            "vsock requires a Linux host with /dev/vhost-vsock "
            "(load it via `sudo modprobe vhost_vsock`)"
        )

    # Pin to Alpine: the SmolVM-*built* image (vsock guest agent + python3 baked
    # in by ImageBuilder, SSH key injected on the kernel cmdline, no cloud-init
    # seed ISO). The QEMU default is Ubuntu, whose cloud qcow2 carries a seed
    # ISO as an extra drive (snapshot then rejects it) and lacks the baked-in
    # agent (vsock never answers) — neither of which is what we want to smoke.
    sandbox = SmolVM(
        backend=e2e_variant.backend,
        os="alpine",
        comm_channel=e2e_variant.transport,
    )
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
