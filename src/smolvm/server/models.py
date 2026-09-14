# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Client-safe wire models for the private SmolVM SDK bridge."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from smolvm.types import BrowserSessionState, CommandResult, VMState, WorkspaceMount

_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$"


class OpenNetworkPolicy(BaseModel):
    """Allow all outbound network access."""

    mode: Literal["open"]


class OffNetworkPolicy(BaseModel):
    """Block outbound network access."""

    mode: Literal["off"]


class RestrictedNetworkPolicy(BaseModel):
    """Allow outbound access only to the supplied IPv4 ranges."""

    mode: Literal["restricted"]
    allowed_cidrs: tuple[str, ...] = Field(min_length=1)


NetworkPolicy = Annotated[
    OpenNetworkPolicy | OffNetworkPolicy | RestrictedNetworkPolicy,
    Field(discriminator="mode"),
]


class CreateSandboxRequest(BaseModel):
    """Create and boot an SDK-session sandbox."""

    image: str | None = Field(
        default=None,
        description="Image reference to boot. Omit to use the published image for the OS.",
    )
    os: Literal["alpine", "ubuntu"] | None = Field(
        default=None,
        description="Guest OS. Ubuntu is the SDK default; Alpine is optional.",
    )
    memory: int | None = Field(default=None, ge=128, le=16384, description="Memory in MiB.")
    disk_size: int | None = Field(
        default=None, ge=1, le=262144, description="Root disk size in MiB."
    )
    backend: Literal["firecracker", "qemu", "libkrun", "vz"] | None = Field(
        default=None,
        description="Optional runtime backend override.",
    )
    network: NetworkPolicy = Field(
        default_factory=lambda: OpenNetworkPolicy(mode="open"),
        description="Outbound network policy.",
    )


class ErrorResponse(BaseModel):
    """The body returned for a handled API error."""

    detail: str = Field(description="Short explanation with a recovery action.")


class SandboxResponse(BaseModel):
    """Public state for one session-owned sandbox."""

    id: str
    status: VMState


class BrowserViewportRequest(BaseModel):
    """Requested browser viewport in CSS pixels."""

    width: int = Field(default=1280, ge=640, le=7680)
    height: int = Field(default=720, ge=480, le=4320)


class CreateBrowserSessionRequest(BaseModel):
    """Create and boot a browser session owned by one SDK client."""

    session_id: str | None = Field(default=None, pattern=_IDENTIFIER_PATTERN)
    mode: Literal["headless", "live"] = "headless"
    backend: Literal["firecracker", "qemu", "libkrun", "auto"] = "auto"
    profile_mode: Literal["ephemeral", "persistent"] = "ephemeral"
    profile_id: str | None = Field(default=None, pattern=_IDENTIFIER_PATTERN)
    timeout_minutes: int = Field(default=30, ge=1, le=240)
    viewport: BrowserViewportRequest = Field(default_factory=BrowserViewportRequest)
    record_video: bool = False
    allow_downloads: bool = True
    memory: int = Field(default=2048, ge=512, le=16384)
    disk_size: int = Field(default=4096, ge=2048, le=16384)
    network: NetworkPolicy = Field(default_factory=lambda: OpenNetworkPolicy(mode="open"))

    @model_validator(mode="after")
    def validate_browser_options(self) -> "CreateBrowserSessionRequest":
        if self.profile_mode == "persistent" and self.profile_id is None:
            raise ValueError("profile_id is required when profile_mode='persistent'")
        if self.record_video and self.mode != "live":
            raise ValueError("record_video requires mode='live'")
        return self


class BrowserSessionResponse(BaseModel):
    """Ready browser session endpoints returned only to the owning SDK client."""

    session_id: str
    sandbox_id: str
    status: BrowserSessionState
    cdp_url: str
    viewer_url: str | None = None
    display_url: str | None = None
    profile_id: str | None = None


class ComputerResourcesRequest(BaseModel):
    """CPU, memory, and disk requested for a Linux computer."""

    memory_mib: int = Field(default=2048, ge=512, le=16384)
    disk_mib: int = Field(default=8192, ge=2048, le=16384)
    vcpus: Literal[2] = 2


class CreateComputerRequest(BaseModel):
    """Create a ready Linux computer owned by one SDK client."""

    computer_id: str | None = Field(default=None, pattern=_IDENTIFIER_PATTERN)
    template: Literal["linux-desktop"] = "linux-desktop"
    backend: Literal["firecracker", "qemu", "auto"] = "auto"
    display: BrowserViewportRequest = Field(
        default_factory=lambda: BrowserViewportRequest(width=1440, height=900)
    )
    resources: ComputerResourcesRequest = Field(default_factory=ComputerResourcesRequest)
    network: NetworkPolicy = Field(default_factory=lambda: OpenNetworkPolicy(mode="open"))
    workspace: list[WorkspaceMount] = Field(default_factory=list)


class ComputerDisplayResponse(BaseModel):
    """Addresses for watching or controlling a computer display."""

    viewer_url: str
    vnc_url: str


class ComputerBrowserResponse(BaseModel):
    """State and automation address for Chromium inside a computer."""

    status: Literal["ready", "closed", "error"]
    cdp_url: str | None


class ComputerResponse(BaseModel):
    """A ready Linux computer and its grouped application endpoints."""

    computer_id: str
    sandbox_id: str
    template: Literal["linux-desktop"]
    status: Literal["ready"]
    capabilities: tuple[
        Literal[
            "display.viewer",
            "display.vnc",
            "browser.cdp",
            "sandbox.exec",
            "sandbox.files",
        ],
        ...,
    ]
    display: ComputerDisplayResponse
    browser: ComputerBrowserResponse


class DesktopResponse(BaseModel):
    """A sanitized loopback display endpoint for a running sandbox."""

    protocol: Literal["vnc"] = "vnc"
    host: Literal["127.0.0.1", "localhost", "::1"]
    port: int = Field(ge=1, le=65535)
    viewer_url: str


class ExecRequest(BaseModel):
    """Run one command inside a sandbox."""

    command: str
    timeout: int = Field(default=30, ge=1, le=3600)
    shell: Literal["login", "raw"] = "login"
    cwd: str | None = Field(default=None, description="Absolute working directory in the guest.")
    env: dict[str, str] = Field(default_factory=dict)


class ExecResponse(CommandResult):
    """Captured command result. Non-zero exits are successful HTTP responses."""

    duration_ms: int = Field(default=0, ge=0)


class CapabilitiesResponse(BaseModel):
    """Runtime protocol and feature discovery."""

    protocol_version: Literal[1] = 1
    capabilities: tuple[str, ...] = (
        "sandbox.create",
        "sandbox.delete",
        "sandbox.exec",
        "files.read",
        "files.write",
        "browser.create",
        "browser.delete",
        "browser.endpoints",
        "browser.exec",
        "browser.files",
        "browser.events",
        "computer.create",
        "computer.delete",
        "computer.endpoints",
        "computer.exec",
        "computer.files",
        "computer.browser",
        "computer.events",
        "events",
        "diagnostics",
    )


class DiagnosticsResponse(BaseModel):
    """Safe local runtime facts suitable for a bug report."""

    protocol_version: Literal[1] = 1
    runtime_version: str
    python_version: str
    platform: str
    supported: bool
    problems: tuple[str, ...] = ()
