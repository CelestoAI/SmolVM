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

"""Tests for the SmolVM HTTP API server.

The handlers are closures created inside :func:`create_app`, so the
tests reach them through ``app.routes`` (each ``APIRoute`` exposes its
``.endpoint``) and call them directly. The :class:`smolvm.SmolVM` facade
is replaced by a stub, so the tests cover the HTTP layer (registry,
error mapping, response shapes) without booting real VMs. This mirrors
``test_dashboard_server.py`` and keeps the suite free of an httpx
dependency.
"""

from collections.abc import Callable

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute

from smolvm import server as server_pkg
from smolvm.exceptions import SmolVMError, VMNotFoundError
from smolvm.server.app import create_app
from smolvm.server.models import CreateSandboxRequest, ExecRequest, SandboxResponse
from smolvm.types import CommandResult, VMState


class FakeSmolVM:
    """Minimal stand-in for the SmolVM facade."""

    last_kwargs: dict | None = None
    start_error: Exception | None = None
    # ids that from_id should reconnect to (simulating VMs that exist on
    # the host but are absent from this app's in-memory registry).
    existing_ids: set[str] = set()
    from_id_calls: int = 0

    def __init__(self, **kwargs: object) -> None:
        FakeSmolVM.last_kwargs = kwargs
        self.vm_id = kwargs.get("vm_id") or "sbx-test"
        self.status = VMState.CREATED

    from_id_error: Exception | None = None

    @classmethod
    def from_id(cls, vm_id: str, **kwargs: object) -> "FakeSmolVM":
        cls.from_id_calls += 1
        if cls.from_id_error is not None:
            raise cls.from_id_error
        if vm_id not in cls.existing_ids:
            raise VMNotFoundError(vm_id)
        return cls(vm_id=vm_id)

    # exec() hooks
    run_error: Exception | None = None
    run_result: CommandResult = CommandResult(exit_code=0, stdout="ok", stderr="")
    last_run_args: tuple | None = None
    # ids deleted via delete() this test
    deleted_ids: set[str] = set()

    def start(self) -> "FakeSmolVM":
        if FakeSmolVM.start_error is not None:
            raise FakeSmolVM.start_error
        self.status = VMState.RUNNING
        return self

    def refresh(self) -> "FakeSmolVM":
        return self

    def run(self, command: str, timeout: int, shell: str) -> CommandResult:
        FakeSmolVM.last_run_args = (command, timeout, shell)
        if FakeSmolVM.run_error is not None:
            raise FakeSmolVM.run_error
        return FakeSmolVM.run_result

    delete_error: Exception | None = None

    def delete(self) -> None:
        if FakeSmolVM.delete_error is not None:
            raise FakeSmolVM.delete_error
        FakeSmolVM.deleted_ids.add(self.vm_id)


def _handler(app: FastAPI, path: str, method: str) -> Callable:
    """Return the endpoint callable for a given route path + method."""
    route = next(
        r for r in app.routes if isinstance(r, APIRoute) and r.path == path and method in r.methods
    )
    return route.endpoint


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A fresh app with the SmolVM facade stubbed out."""
    FakeSmolVM.last_kwargs = None
    FakeSmolVM.start_error = None
    FakeSmolVM.existing_ids = set()
    FakeSmolVM.from_id_calls = 0
    FakeSmolVM.from_id_error = None
    FakeSmolVM.run_error = None
    FakeSmolVM.run_result = CommandResult(exit_code=0, stdout="ok", stderr="")
    FakeSmolVM.last_run_args = None
    FakeSmolVM.deleted_ids = set()
    FakeSmolVM.delete_error = None
    monkeypatch.setattr("smolvm.server.app.SmolVM", FakeSmolVM)
    # list_sandboxes enumerates the host directly; back it by the stub's ids.
    monkeypatch.setattr("smolvm.server.app._existing_vm_ids", lambda: set(FakeSmolVM.existing_ids))
    return create_app()


def test_create_sandbox_returns_running_state(app: FastAPI) -> None:
    create = _handler(app, "/sandboxes", "POST")

    result = create(CreateSandboxRequest(os="ubuntu", memory=1024))

    assert isinstance(result, SandboxResponse)
    assert result.id == "sbx-test"
    assert result.status is VMState.RUNNING
    # Only the fields the caller set are forwarded to the facade.
    assert FakeSmolVM.last_kwargs == {"os": "ubuntu", "memory": 1024}


def test_create_sandbox_defaults_when_body_empty(app: FastAPI) -> None:
    create = _handler(app, "/sandboxes", "POST")

    create(CreateSandboxRequest())

    # Nothing set -> no kwargs forwarded; the facade applies its defaults.
    assert FakeSmolVM.last_kwargs == {}


def test_create_sandbox_maps_facade_error_to_400(app: FastAPI) -> None:
    FakeSmolVM.start_error = SmolVMError("image does not support SSH")
    create = _handler(app, "/sandboxes", "POST")

    with pytest.raises(HTTPException) as exc_info:
        create(CreateSandboxRequest())

    assert exc_info.value.status_code == 400
    assert "image does not support SSH" in exc_info.value.detail


def test_get_sandbox_after_create(app: FastAPI) -> None:
    create = _handler(app, "/sandboxes", "POST")
    get = _handler(app, "/sandboxes/{sandbox_id}", "GET")

    created = create(CreateSandboxRequest())
    fetched = get(created.id)

    assert fetched.id == created.id


def test_get_sandbox_rehydrates_from_host_on_registry_miss(app: FastAPI) -> None:
    # A sandbox that exists on the host but not in this app's registry
    # (e.g. created before this server process started) is reconnected
    # via SmolVM.from_id rather than 404ing.
    FakeSmolVM.existing_ids = {"sbx-preexisting"}
    get = _handler(app, "/sandboxes/{sandbox_id}", "GET")

    fetched = get("sbx-preexisting")

    assert fetched.id == "sbx-preexisting"
    assert FakeSmolVM.from_id_calls == 1

    # The reconnect backfills the registry, so a second GET hits the cache
    # without calling from_id again — proven by clearing existing_ids
    # (from_id would now raise VMNotFoundError if it were called).
    FakeSmolVM.existing_ids.clear()
    fetched_again = get("sbx-preexisting")

    assert fetched_again.id == "sbx-preexisting"
    assert FakeSmolVM.from_id_calls == 1


def test_get_sandbox_maps_reconnect_failure_to_409(app: FastAPI) -> None:
    # The sandbox exists on the host but reconnecting fails (bad state,
    # unreachable control channel) -> a state conflict, not a 404.
    FakeSmolVM.from_id_error = SmolVMError("control channel unreachable")
    get = _handler(app, "/sandboxes/{sandbox_id}", "GET")

    with pytest.raises(HTTPException) as exc_info:
        get("sbx-broken")

    assert exc_info.value.status_code == 409
    # Message names the sandbox and a recovery command, not the raw
    # internal exception text.
    assert "sbx-broken" in exc_info.value.detail
    assert "could not be reconnected" in exc_info.value.detail


def test_get_unknown_sandbox_returns_404(app: FastAPI) -> None:
    get = _handler(app, "/sandboxes/{sandbox_id}", "GET")

    with pytest.raises(HTTPException) as exc_info:
        get("does-not-exist")

    assert exc_info.value.status_code == 404
    assert "does-not-exist" in exc_info.value.detail


def test_list_sandboxes_enumerates_the_host(app: FastAPI) -> None:
    # list reflects every sandbox on the host, sorted, regardless of
    # whether it is already in the in-memory registry.
    FakeSmolVM.existing_ids = {"sbx-b", "sbx-a"}
    list_all = _handler(app, "/sandboxes", "GET")

    result = list_all()

    assert [s.id for s in result] == ["sbx-a", "sbx-b"]
    assert all(isinstance(s, SandboxResponse) for s in result)


def test_list_sandboxes_empty_when_no_host_vms(app: FastAPI) -> None:
    list_all = _handler(app, "/sandboxes", "GET")
    assert list_all() == []


def test_delete_sandbox_stops_and_evicts(app: FastAPI) -> None:
    create = _handler(app, "/sandboxes", "POST")
    delete = _handler(app, "/sandboxes/{sandbox_id}", "DELETE")
    get = _handler(app, "/sandboxes/{sandbox_id}", "GET")

    created = create(CreateSandboxRequest())
    response = delete(created.id)

    assert response.status_code == 204
    assert created.id in FakeSmolVM.deleted_ids
    # Evicted from the registry: a later GET no longer hits the cache and,
    # with no host VM to reconnect to, 404s.
    with pytest.raises(HTTPException) as exc_info:
        get(created.id)
    assert exc_info.value.status_code == 404


def test_delete_unknown_sandbox_returns_404(app: FastAPI) -> None:
    delete = _handler(app, "/sandboxes/{sandbox_id}", "DELETE")

    with pytest.raises(HTTPException) as exc_info:
        delete("does-not-exist")

    assert exc_info.value.status_code == 404


def test_delete_maps_delete_failure_to_409(app: FastAPI) -> None:
    # The sandbox resolves but tearing it down fails -> a state conflict,
    # not an unhandled 500.
    create = _handler(app, "/sandboxes", "POST")
    delete = _handler(app, "/sandboxes/{sandbox_id}", "DELETE")
    FakeSmolVM.delete_error = SmolVMError("disk is busy")

    created = create(CreateSandboxRequest())
    with pytest.raises(HTTPException) as exc_info:
        delete(created.id)

    assert exc_info.value.status_code == 409


def test_exec_command_returns_result(app: FastAPI) -> None:
    create = _handler(app, "/sandboxes", "POST")
    exec_cmd = _handler(app, "/sandboxes/{sandbox_id}/exec", "POST")
    FakeSmolVM.run_result = CommandResult(exit_code=0, stdout="hello\n", stderr="")

    created = create(CreateSandboxRequest())
    result = exec_cmd(created.id, ExecRequest(command="echo hello"))

    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    # Request fields are forwarded verbatim to the facade.
    assert FakeSmolVM.last_run_args == ("echo hello", 30, "login")


def test_exec_command_nonzero_exit_is_still_200(app: FastAPI) -> None:
    # A command that runs and fails is a successful exec, not an HTTP error.
    create = _handler(app, "/sandboxes", "POST")
    exec_cmd = _handler(app, "/sandboxes/{sandbox_id}/exec", "POST")
    FakeSmolVM.run_result = CommandResult(exit_code=1, stdout="", stderr="nope")

    created = create(CreateSandboxRequest())
    result = exec_cmd(created.id, ExecRequest(command="false"))

    assert result.exit_code == 1
    assert result.stderr == "nope"


def test_exec_command_maps_run_failure_to_409(app: FastAPI) -> None:
    create = _handler(app, "/sandboxes", "POST")
    exec_cmd = _handler(app, "/sandboxes/{sandbox_id}/exec", "POST")
    FakeSmolVM.run_error = SmolVMError("sandbox is not running")

    created = create(CreateSandboxRequest())
    with pytest.raises(HTTPException) as exc_info:
        exec_cmd(created.id, ExecRequest(command="echo hi"))

    assert exc_info.value.status_code == 409
    # Message names the sandbox and a recovery command, not the raw
    # internal exception text.
    assert "sbx-test" in exc_info.value.detail
    assert "could not run" in exc_info.value.detail


def test_exec_unknown_sandbox_returns_404(app: FastAPI) -> None:
    exec_cmd = _handler(app, "/sandboxes/{sandbox_id}/exec", "POST")

    with pytest.raises(HTTPException) as exc_info:
        exec_cmd("does-not-exist", ExecRequest(command="echo hi"))

    assert exc_info.value.status_code == 404


def test_openapi_exposes_clean_operation_ids(app: FastAPI) -> None:
    spec = app.openapi()
    operation_ids = {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    assert {
        "createSandbox",
        "getSandbox",
        "listSandboxes",
        "deleteSandbox",
        "execCommand",
    } <= operation_ids


def test_package_exports_create_app() -> None:
    assert server_pkg.create_app is create_app
