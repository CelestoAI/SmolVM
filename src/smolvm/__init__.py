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

"""SmolVM.

A Python SDK for running AI agents and executing untrusted code in a secure,
sandboxed environment.
"""

from importlib.metadata import version as _pkg_version

from smolvm.callbacks import Callback, CommandBlockedError, RunContext
from smolvm.exceptions import (
    CommandExecutionUnavailableError,
    FirecrackerAPIError,
    HostError,
    ImageError,
    NetworkError,
    OperationTimeoutError,
    SmolVMError,
    SnapshotAlreadyExistsError,
    SnapshotNotFoundError,
    ValidationError,
    VMAlreadyExistsError,
    VMNotFoundError,
)
from smolvm.facade import SmolVM
from smolvm.host.manager import HostManager
from smolvm.images.boot import BootImage, DirectKernelBoot, FirmwareBoot
from smolvm.images.builder import SSH_BOOT_ARGS, DockerRootfsBuilder, ImageBuilder
from smolvm.images.manager import ImageManager, ImageSource, LocalImage, S3ImageManifest, S3ImageRef
from smolvm.kernels import ensure_base_kernel_for_backend
from smolvm.ssh import SSHClient
from smolvm.types import (
    BrowserViewport,
    CommandResult,
    DesktopEndpoint,
    DisplaySandboxProtocol,
    GuestFlushPolicy,
    GuestOS,
    InternetSettings,
    MacOSMachineConfig,
    NetworkAttachmentConfig,
    NetworkConfig,
    QemuMachine,
    SnapshotArtifacts,
    SnapshotCapturePolicy,
    SnapshotInfo,
    SnapshotType,
    VMConfig,
    VMInfo,
    VMState,
    WorkspaceMount,
)
from smolvm.vm import SmolVMManager

__version__ = _pkg_version("smolvm")

__all__ = [
    # Core classes
    "SmolVM",
    "SmolVMManager",
    # Callbacks / hooks
    "Callback",
    "RunContext",
    "CommandBlockedError",
    # Image management
    "ImageManager",
    "ImageBuilder",
    "DockerRootfsBuilder",
    "BootImage",
    "DirectKernelBoot",
    "FirmwareBoot",
    "SSH_BOOT_ARGS",
    "ImageSource",
    "LocalImage",
    "S3ImageManifest",
    "S3ImageRef",
    "ensure_base_kernel_for_backend",
    # Host setup
    "HostManager",
    # SSH
    "SSHClient",
    # Data models
    "InternetSettings",
    "NetworkAttachmentConfig",
    "VMConfig",
    "VMInfo",
    "VMState",
    "NetworkConfig",
    "QemuMachine",
    "SnapshotArtifacts",
    "SnapshotCapturePolicy",
    "SnapshotInfo",
    "SnapshotType",
    "GuestFlushPolicy",
    "CommandResult",
    "DesktopEndpoint",
    "DisplaySandboxProtocol",
    "BrowserViewport",
    "WorkspaceMount",
    "GuestOS",
    "MacOSMachineConfig",
    # Exceptions
    "SmolVMError",
    "CommandExecutionUnavailableError",
    "SnapshotAlreadyExistsError",
    "SnapshotNotFoundError",
    "ValidationError",
    "VMAlreadyExistsError",
    "VMNotFoundError",
    "NetworkError",
    "HostError",
    "ImageError",
    "FirecrackerAPIError",
    "OperationTimeoutError",
]
