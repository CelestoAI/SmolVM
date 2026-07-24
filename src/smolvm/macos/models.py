# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Private request/result models shared by macOS runtime drivers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from smolvm.types import DesktopEndpoint, WorkspaceMount


def _validate_lume_name(name: str) -> None:
    """Reject names Lume could interpret as command options."""
    if name.startswith("-"):
        raise ValueError("macOS runtime names cannot begin with '-'")


class LumeDiskSize(BaseModel):
    """Allocated and logical disk sizes reported by Lume."""

    allocated: int = Field(ge=0)
    total: int = Field(ge=0)


class LumeSharedDirectory(BaseModel):
    """One host directory reported by Lume."""

    host_path: str = Field(alias="hostPath")
    tag: str
    read_only: bool = Field(alias="readOnly")


class LumeVMDetails(BaseModel):
    """Pinned subset of Lume 0.4's JSON VM details schema."""

    name: str
    os: str
    cpu_count: int = Field(ge=1, alias="cpuCount")
    memory_size: int = Field(ge=1, alias="memorySize")
    disk_size: LumeDiskSize = Field(alias="diskSize")
    display: str
    status: str
    provisioning_operation: str | None = Field(alias="provisioningOperation")
    vnc_url: str | None = Field(alias="vncUrl")
    ip_address: str | None = Field(alias="ipAddress")
    ssh_available: bool | None = Field(alias="sshAvailable")
    location_name: str = Field(alias="locationName")
    shared_directories: list[LumeSharedDirectory] | None = Field(alias="sharedDirectories")
    network_mode: str | None = Field(alias="networkMode")
    download_progress: object | None = Field(alias="downloadProgress")


@dataclass(frozen=True, slots=True)
class MacOSInstallProgress:
    """One machine-readable image preparation progress update."""

    phase: Literal["download", "install", "setup", "complete"]
    percent: int | None = None


@dataclass(frozen=True, slots=True)
class MacOSInstallRequest:
    """Inputs for installing one reusable macOS base machine."""

    name: str
    storage_path: Path
    ipsw: Literal["latest"] | Path = "latest"
    unattended_preset: str = "tahoe"
    cpu_count: int = 4
    memory_mib: int = 8192
    disk_size_gib: int = 80
    display_width: int = 1440
    display_height: int = 900

    def __post_init__(self) -> None:
        _validate_lume_name(self.name)


@dataclass(frozen=True, slots=True)
class MacOSRunRequest:
    """Inputs for starting one cloned macOS machine."""

    name: str
    storage_path: Path
    workspace_mounts: tuple[WorkspaceMount, ...] = ()

    def __post_init__(self) -> None:
        _validate_lume_name(self.name)


@dataclass(frozen=True, slots=True)
class MacOSLaunchResult:
    """Runtime state returned after a macOS display becomes available."""

    pid: int
    display: DesktopEndpoint
    ip_address: str | None
    vnc_password: str = field(repr=False)
