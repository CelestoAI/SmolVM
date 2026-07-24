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

"""Wire models for the SmolVM HTTP API.

These are deliberately a *sanitized* view of the engine's internal
types in :mod:`smolvm.types`. The internal :class:`~smolvm.types.VMInfo`
carries host-only fields (filesystem ``Path`` values, process IDs, TAP
device names) that are meaningless and leaky to an HTTP client, so the
API speaks in these smaller models instead. Inner value types that are
already client-safe — such as :class:`~smolvm.types.CommandResult` and
the :class:`~smolvm.types.VMState` enum — are reused directly so the
generated SDKs stay in sync with the engine.
"""

from typing import Literal

from pydantic import BaseModel, Field

from smolvm.types import CommandResult, VMState


class CreateSandboxRequest(BaseModel):
    """Request body for creating (and booting) a sandbox.

    Mirrors the auto-config arguments of the :class:`smolvm.SmolVM`
    constructor. All fields are optional; omitting them boots the
    default Alpine micro-VM.
    """

    image: str | None = Field(
        default=None,
        description="Image reference to boot (S3 ref, file:// URI, or path). "
        "Omit to use the default built-in image.",
    )
    os: Literal["alpine", "ubuntu", "windows", "macos"] | None = Field(
        default=None,
        description=(
            "Guest OS for auto-configured images: 'alpine', 'ubuntu', 'windows', or 'macos'."
        ),
    )
    memory: int | None = Field(
        default=None,
        ge=128,
        le=16384,
        description="Guest memory in MiB.",
    )
    disk_size: int | None = Field(
        default=None,
        ge=1,
        le=262144,
        description="Guest disk size in MiB.",
    )
    backend: Literal["firecracker", "qemu", "libkrun", "vz"] | None = Field(
        default=None,
        description="Runtime backend override: 'firecracker', 'qemu', 'libkrun', or 'vz'.",
    )


class ErrorResponse(BaseModel):
    """The body returned for a handled 4xx error.

    Mirrors FastAPI's :class:`~fastapi.HTTPException` shape (``{detail}``)
    so the generated SDKs get a typed error surface distinct from the
    422 request-validation body, whose ``detail`` is a list of field
    errors rather than a single string.
    """

    detail: str = Field(description="Human-readable explanation of the error.")


class SandboxResponse(BaseModel):
    """A sandbox's public, client-safe state.

    Host-internal details (disk paths, PID, network device) are
    intentionally omitted — clients address a sandbox only by ``id``.
    """

    id: str = Field(description="Stable sandbox identifier.")
    status: VMState = Field(description="Current lifecycle state.")


class DesktopResponse(BaseModel):
    """A sanitized loopback display endpoint for a running sandbox."""

    protocol: Literal["vnc"] = "vnc"
    host: Literal["127.0.0.1", "localhost", "::1"]
    port: int = Field(ge=1, le=65535)
    viewer_url: str


class ExecRequest(BaseModel):
    """Request body for running a command inside a sandbox.

    Mirrors the arguments of :meth:`smolvm.SmolVM.run`.
    """

    command: str = Field(description="Shell command to execute in the sandbox.")
    timeout: int = Field(
        default=30,
        ge=1,
        le=3600,
        description="Maximum seconds to wait for the command to finish.",
    )
    shell: Literal["login", "raw"] = Field(
        default="login",
        description="'login' runs via the guest login shell; 'raw' executes "
        "the command directly with no shell wrapping.",
    )


class ExecResponse(CommandResult):
    """The result of a command run inside a sandbox.

    Reuses the engine's :class:`~smolvm.types.CommandResult` (exit code,
    stdout, stderr) under an API-owned name so the generated SDKs expose
    a stable ``ExecResponse`` type rather than an engine-internal one.
    """
