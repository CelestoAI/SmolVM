"""Tests for the sanitized HTTP API wire models."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from pydantic import ValidationError

from smolvm.server.models import (
    CreateSandboxRequest,
    DesktopResponse,
    ErrorResponse,
    ExecRequest,
    ExecResponse,
    SandboxResponse,
)
from smolvm.types import VMState


def test_create_request_accepts_supported_options() -> None:
    request = CreateSandboxRequest(os="ubuntu", memory=1024, disk_size=4096, backend="qemu")

    assert request.model_dump(exclude_none=True) == {
        "os": "ubuntu",
        "memory": 1024,
        "disk_size": 4096,
        "backend": "qemu",
    }


@pytest.mark.parametrize("field,value", [("memory", 127), ("disk_size", 0)])
def test_create_request_rejects_invalid_resource_sizes(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        CreateSandboxRequest(**{field: value})


def test_public_response_models_validate_and_serialize() -> None:
    desktop = DesktopResponse(port=5901, viewer_url="vnc://127.0.0.1:5901", host="127.0.0.1")
    sandbox = SandboxResponse(id="sbx-test", status=VMState.RUNNING)
    error = ErrorResponse(detail="try again")
    request = ExecRequest(command="echo hi", timeout=10, shell="raw")
    result = ExecResponse(exit_code=0, stdout="hi\n", stderr="")

    assert desktop.protocol == "vnc"
    assert sandbox.model_dump() == {"id": "sbx-test", "status": VMState.RUNNING}
    assert error.detail == "try again"
    assert request.shell == "raw"
    assert result.exit_code == 0
