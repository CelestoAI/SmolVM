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

"""SmolVM - Production-ready Python SDK for sandbox microVM management."""

from smolvm.build import SSH_BOOT_ARGS, ImageBuilder
from smolvm.exceptions import (
    CommandExecutionUnavailableError,
    FirecrackerAPIError,
    HostError,
    ImageError,
    NetworkError,
    OperationTimeoutError,
    SmolVMError,
    TimeoutError,  # Backward compatibility alias
    ValidationError,
    VMAlreadyExistsError,
    VMNotFoundError,
)
from smolvm.facade import SmolVM
from smolvm.host import HostManager
from smolvm.images import ImageManager, ImageSource, LocalImage
from smolvm.ssh import SSHClient
from smolvm.types import CommandResult, NetworkConfig, VMConfig, VMInfo, VMState
from smolvm.vm import SmolVMManager

__version__ = "0.0.5.a0"

__all__ = [
    # Core classes
    "SmolVM",
    "SmolVMManager",
    # Image management
    "ImageManager",
    "ImageBuilder",
    "SSH_BOOT_ARGS",
    "ImageSource",
    "LocalImage",
    # Host setup
    "HostManager",
    # SSH
    "SSHClient",
    # Data models
    "VMConfig",
    "VMInfo",
    "VMState",
    "NetworkConfig",
    "CommandResult",
    # Exceptions
    "SmolVMError",
    "CommandExecutionUnavailableError",
    "ValidationError",
    "VMAlreadyExistsError",
    "VMNotFoundError",
    "NetworkError",
    "HostError",
    "ImageError",
    "FirecrackerAPIError",
    "OperationTimeoutError",
    "TimeoutError",  # Alias
]
