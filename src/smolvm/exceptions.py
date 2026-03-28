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

"""Exception hierarchy for SmolVM SDK."""


class SmolVMError(Exception):
    """Base exception for all SmolVM errors."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationError(SmolVMError):
    """Raised when input validation fails."""

    pass


class VMAlreadyExistsError(SmolVMError):
    """Raised when attempting to create a VM with an existing ID."""

    def __init__(self, vm_id: str) -> None:
        super().__init__(f"VM '{vm_id}' already exists", {"vm_id": vm_id})
        self.vm_id = vm_id


class VMNotFoundError(SmolVMError):
    """Raised when a VM is not found."""

    def __init__(self, vm_id: str) -> None:
        super().__init__(f"VM '{vm_id}' not found", {"vm_id": vm_id})
        self.vm_id = vm_id


class BrowserSessionAlreadyExistsError(SmolVMError):
    """Raised when attempting to create a browser session with an existing ID."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Browser session '{session_id}' already exists",
            {"session_id": session_id},
        )
        self.session_id = session_id


class BrowserSessionNotFoundError(SmolVMError):
    """Raised when a browser session is not found."""

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Browser session '{session_id}' not found",
            {"session_id": session_id},
        )
        self.session_id = session_id


class NetworkError(SmolVMError):
    """Raised when network operations fail (TAP, NAT, IP allocation)."""

    pass


class HostError(SmolVMError):
    """Raised when host environment checks fail (KVM, dependencies, Firecracker)."""

    pass


class ImageError(SmolVMError):
    """Raised when image operations fail (download, checksum, cache)."""

    pass


class FirecrackerAPIError(SmolVMError):
    """Raised when Firecracker API calls fail."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message, {"status_code": status_code})
        self.status_code = status_code


class OperationTimeoutError(SmolVMError):
    """Raised when an operation times out."""

    def __init__(self, operation: str, timeout_seconds: float) -> None:
        super().__init__(
            f"Operation '{operation}' timed out after {timeout_seconds}s",
            {"operation": operation, "timeout_seconds": timeout_seconds},
        )
        self.operation = operation
        self.timeout_seconds = timeout_seconds


class CommandExecutionUnavailableError(SmolVMError):
    """Raised when command execution is not available for a VM profile."""

    def __init__(
        self,
        vm_id: str,
        reason: str,
        remediation: str | None = None,
    ) -> None:
        message = f"Cannot run command in VM '{vm_id}': {reason}"
        if remediation:
            message = f"{message}\n{remediation}"
        super().__init__(message, {"vm_id": vm_id, "reason": reason})
        self.vm_id = vm_id
        self.reason = reason
        self.remediation = remediation


# Backwards-compatible alias
TimeoutError = OperationTimeoutError
