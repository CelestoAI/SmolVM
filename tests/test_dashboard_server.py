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

"""Tests for dashboard FastAPI server logic."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from smolvm.dashboard import server
from smolvm.exceptions import VMNotFoundError
from smolvm.types import VMConfig, VMInfo, VMState

_KERNEL = Path(__file__).resolve()
_ROOTFS = Path(__file__).resolve()


class DummyStateManager:
    """StateManager stub for server endpoint tests."""

    def list_vms(self, status: object = None) -> list[object]:
        return []

    def get_vm(self, vm_id: str) -> object:
        raise VMNotFoundError(vm_id)


class DummySDK:
    """SDK stub for command endpoint tests."""


class _FakeResponse:
    """Tiny requests.Response test double."""

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def test_latest_dashboard_release_asset_prefers_exact_tag_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asset selection should prioritize the exact <prefix><tag>.tar.gz name."""
    payload = [
        {
            "tag_name": "v0.0.4",
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.3.tar.gz",
                    "browser_download_url": "https://example.invalid/old.tar.gz",
                },
                {
                    "name": "smolvm-dashboard-ui-v0.0.4.tar.gz",
                    "browser_download_url": "https://example.invalid/new.tar.gz",
                },
            ],
        }
    ]
    monkeypatch.setattr(server.requests, "get", lambda *args, **kwargs: _FakeResponse(payload))

    tag, url = server._latest_dashboard_release_asset()

    assert tag == "v0.0.4"
    assert url == "https://example.invalid/new.tar.gz"


def test_latest_dashboard_release_asset_skips_prerelease_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default behavior should prefer stable assets over prerelease ones."""
    payload = [
        {
            "tag_name": "v0.0.5.a0",
            "prerelease": True,
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.5.a0.tar.gz",
                    "browser_download_url": "https://example.invalid/prerelease.tar.gz",
                }
            ],
        },
        {
            "tag_name": "v0.0.4",
            "prerelease": False,
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.4.tar.gz",
                    "browser_download_url": "https://example.invalid/stable.tar.gz",
                }
            ],
        },
    ]
    monkeypatch.setattr(server.requests, "get", lambda *args, **kwargs: _FakeResponse(payload))

    tag, url = server._latest_dashboard_release_asset()

    assert tag == "v0.0.4"
    assert url == "https://example.invalid/stable.tar.gz"


def test_latest_dashboard_release_asset_allows_prerelease_with_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """allow_prerelease=True should allow selecting prerelease assets."""
    payload = [
        {
            "tag_name": "v0.0.5.a0",
            "prerelease": True,
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.5.a0.tar.gz",
                    "browser_download_url": "https://example.invalid/prerelease.tar.gz",
                }
            ],
        },
        {
            "tag_name": "v0.0.4",
            "prerelease": False,
            "assets": [
                {
                    "name": "smolvm-dashboard-ui-v0.0.4.tar.gz",
                    "browser_download_url": "https://example.invalid/stable.tar.gz",
                }
            ],
        },
    ]
    monkeypatch.setattr(server.requests, "get", lambda *args, **kwargs: _FakeResponse(payload))

    tag, url = server._latest_dashboard_release_asset(allow_prerelease=True)

    assert tag == "v0.0.5.a0"
    assert url == "https://example.invalid/prerelease.tar.gz"


def test_resolve_ui_dist_path_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """SMOLVM_DASHBOARD_UI_DIST should override default dist path resolution."""
    custom = Path("/tmp/custom-ui-dist")
    monkeypatch.setenv(server.UI_DIST_ENV, str(custom))

    assert server._resolve_ui_dist_path() == custom.resolve()


def test_allow_beta_releases_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ALLOW_BETA_ENV should support common truthy/falsey values."""
    monkeypatch.setenv(server.ALLOW_BETA_ENV, "true")
    assert server._allow_beta_releases() is True

    monkeypatch.setenv(server.ALLOW_BETA_ENV, "0")
    assert server._allow_beta_releases() is False


def test_resolve_ui_dist_path_uses_state_dir_when_repo_layout_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Installed-package layout should fallback to resolve_data_dir()."""
    fake_server = tmp_path / "site-packages" / "smolvm" / "dashboard" / "server.py"
    fake_server.parent.mkdir(parents=True)
    fake_server.write_text("", encoding="utf-8")

    state_dir = tmp_path / "state"

    monkeypatch.delenv(server.UI_DIST_ENV, raising=False)
    monkeypatch.setattr(server, "__file__", str(fake_server))
    monkeypatch.setattr(server, "resolve_data_dir", lambda: state_dir)

    assert server._resolve_ui_dist_path() == state_dir / "dashboard-ui" / "dist"


def test_list_vms_invalid_status_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown status query values should map to 400, not 500."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.list_vms(status="bogus"))

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid status: bogus"


def test_execute_command_route_uses_command_response_model() -> None:
    """POST /api/command should declare CommandResponse as its response model."""
    route = next(
        r
        for r in server.app.routes
        if isinstance(r, APIRoute) and r.path == "/api/command" and "POST" in r.methods
    )

    assert route.response_model is server.CommandResponse


class _FleetStateManager:
    """State manager holding a fixed set of running sandboxes."""

    def __init__(self, vm_ids: list[str], status: VMState = VMState.RUNNING) -> None:
        self._vm_ids = vm_ids
        self._status = status

    def _vm(self, vm_id: str) -> VMInfo:
        return VMInfo(
            vm_id=vm_id,
            status=self._status,
            config=VMConfig(vm_id=vm_id, kernel_path=_KERNEL, rootfs_path=_ROOTFS),
        )

    def list_vms(self, status: object = None) -> list[VMInfo]:
        return [self._vm(vm_id) for vm_id in self._vm_ids]

    def get_vm(self, vm_id: str) -> VMInfo:
        if vm_id in self._vm_ids:
            return self._vm(vm_id)
        raise VMNotFoundError(vm_id)


def _sdk_returning_unchanged(status: VMState = VMState.CREATED) -> object:
    """An SDK whose stop mirrors the manager for a sandbox that is not running."""

    class _SDK:
        def stop(self, vm_id: str) -> VMInfo:
            return VMInfo(
                vm_id=vm_id,
                status=status,
                config=VMConfig(vm_id=vm_id, kernel_path=_KERNEL, rootfs_path=_ROOTFS),
            )

    return _SDK()


def _sdk_where(behaviour: object) -> object:
    """An SDK whose stop/delete run *behaviour* for each sandbox id."""

    class _SDK:
        def stop(self, vm_id: str) -> VMInfo:
            behaviour(vm_id)  # type: ignore[operator]
            return VMInfo(
                vm_id=vm_id,
                status=VMState.STOPPED,
                config=VMConfig(vm_id=vm_id, kernel_path=_KERNEL, rootfs_path=_ROOTFS),
            )

        def delete(self, vm_id: str) -> None:
            behaviour(vm_id)  # type: ignore[operator]

    return _SDK()


@pytest.mark.parametrize("verb", ["stop", "delete"])
def test_execute_command_reports_a_command_that_did_nothing(
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    """If every sandbox failed, the command did nothing and must say so.

    "Stopped 0 VMs." is technically true but renders as success and clears
    the input, hiding a total failure behind a plausible count.
    """

    def boom(_vm_id: str) -> None:
        raise RuntimeError("hypervisor is wedged")

    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _FleetStateManager(["sbx-1"]))
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _sdk_where(boom))

    text = f"{verb} all" + (" --force" if verb == "delete" else "")
    response = asyncio.run(server.execute_command(server.CommandRequest(text=text)))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    error = json.loads(response.body)["error"]
    # Names the sandbox and an exact command, per the user-facing message rules.
    assert f"Could not {verb} 'sbx-1'" in error
    assert f"Run '{verb} sbx-1' to try again" in error


@pytest.mark.parametrize("verb,past", [("stop", "Stopped"), ("delete", "Deleted")])
def test_execute_command_reports_a_partial_failure_honestly(
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    past: str,
) -> None:
    """A count alone would hide the sandboxes that did not do what was asked."""

    def one_fails(vm_id: str) -> None:
        if vm_id == "sbx-2":
            raise RuntimeError("wedged")

    monkeypatch.setattr(
        server, "_get_state_manager", lambda _app: _FleetStateManager(["sbx-1", "sbx-2"])
    )
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _sdk_where(one_fails))

    text = f"{verb} all" + (" --force" if verb == "delete" else "")
    response = asyncio.run(server.execute_command(server.CommandRequest(text=text)))

    assert isinstance(response, server.CommandResponse)
    assert response.result == (
        f"{past} 1 of 2 sandboxes. 'sbx-2' failed: run '{verb} sbx-2' to try again."
    )
    assert response.affected_vms == ["sbx-1"]


def test_execute_command_caps_the_failed_names_it_prints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wide command must not print a wall of ids at the user."""

    def boom(_vm_id: str) -> None:
        raise RuntimeError("wedged")

    fleet = [f"sbx-{n}" for n in range(1, 7)]
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _FleetStateManager(fleet))
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _sdk_where(boom))

    response = asyncio.run(server.execute_command(server.CommandRequest(text="stop all")))

    assert isinstance(response, JSONResponse)
    error = json.loads(response.body)["error"]
    assert "'sbx-1', 'sbx-2', 'sbx-3' and 3 more" in error
    assert "sbx-4" not in error
    assert "Run 'stop sbx-1' to try again" in error


@pytest.mark.parametrize("verb,past", [("stop", "Stopped"), ("delete", "Deleted")])
def test_execute_command_treats_an_already_gone_sandbox_as_done(
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    past: str,
) -> None:
    """A sandbox that vanished mid-command is already in the state asked for."""

    def vanished(vm_id: str) -> None:
        raise VMNotFoundError(vm_id)

    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _FleetStateManager(["sbx-1"]))
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _sdk_where(vanished))

    text = f"{verb} sbx-1" + (" --force" if verb == "delete" else "")
    response = asyncio.run(server.execute_command(server.CommandRequest(text=text)))

    assert isinstance(response, server.CommandResponse)
    assert response.result == f"{past} 1 VMs."


@pytest.mark.parametrize("prior", [VMState.CREATED, VMState.STOPPED, VMState.ERROR])
def test_execute_command_does_not_claim_to_stop_a_sandbox_that_was_not_running(
    monkeypatch: pytest.MonkeyPatch,
    prior: VMState,
) -> None:
    """The manager returns the record unchanged when there was nothing to stop.

    Every state that is not RUNNING or PAUSED behaves this way, so the returned
    record proves nothing on its own. ``STOPPED`` is the trap: the record comes
    back reading ``STOPPED`` without this command having stopped anything.
    """
    monkeypatch.setattr(
        server,
        "_get_state_manager",
        lambda _app: _FleetStateManager(["sbx-idle"], status=prior),
    )
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _sdk_returning_unchanged(prior))

    response = asyncio.run(server.execute_command(server.CommandRequest(text="stop sbx-idle")))

    assert isinstance(response, server.CommandResponse)
    assert response.result == "Stopped 0 of 1 sandboxes. 'sbx-idle' was not running."
    assert response.affected_vms == []


def test_execute_command_does_not_claim_to_stop_an_idle_sandbox_that_vanished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vanishing is not stopping if the sandbox was not running to begin with.

    "Already gone is the state you asked for" is true for delete. For stop it
    only holds when the sandbox was actually running when the command started.
    """

    class _VanishedSDK:
        def stop(self, vm_id: str) -> VMInfo:
            raise VMNotFoundError(vm_id)

    monkeypatch.setattr(
        server,
        "_get_state_manager",
        lambda _app: _FleetStateManager(["sbx-idle"], status=VMState.STOPPED),
    )
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _VanishedSDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text="stop sbx-idle")))

    assert isinstance(response, server.CommandResponse)
    assert response.result == "Stopped 0 of 1 sandboxes. 'sbx-idle' was not running."
    assert response.affected_vms == []


def test_execute_command_errors_when_only_idle_sandboxes_survive_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing stopped and something failed, so this is not a result.

    A sandbox that was merely idle must not soften a real failure into a 200
    that renders as success and clears the command.
    """

    class _MixedSDK:
        def stop(self, vm_id: str) -> VMInfo:
            if vm_id == "sbx-1":
                return VMInfo(
                    vm_id=vm_id,
                    status=VMState.CREATED,
                    config=VMConfig(vm_id=vm_id, kernel_path=_KERNEL, rootfs_path=_ROOTFS),
                )
            raise RuntimeError("wedged")

    class _MixedFleet(_FleetStateManager):
        def _vm(self, vm_id: str) -> VMInfo:
            status = VMState.CREATED if vm_id == "sbx-1" else VMState.RUNNING
            return VMInfo(
                vm_id=vm_id,
                status=status,
                config=VMConfig(vm_id=vm_id, kernel_path=_KERNEL, rootfs_path=_ROOTFS),
            )

    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _MixedFleet(["sbx-1", "sbx-2"]))
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _MixedSDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text="stop all")))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    assert "Could not stop 'sbx-2'" in json.loads(response.body)["error"]


def test_execute_command_reports_a_running_sandbox_that_did_not_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sandbox that was running and stayed running is a failure, not a no-op."""

    class _StubbornSDK:
        def stop(self, vm_id: str) -> VMInfo:
            return VMInfo(
                vm_id=vm_id,
                status=VMState.RUNNING,
                config=VMConfig(vm_id=vm_id, kernel_path=_KERNEL, rootfs_path=_ROOTFS),
            )

    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _FleetStateManager(["sbx-1"]))
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _StubbornSDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text="stop sbx-1")))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 500
    assert "Could not stop 'sbx-1'" in json.loads(response.body)["error"]


def test_execute_command_group_delete_needs_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting a whole group is irreversible, so one line must not do it.

    The CLI confirms the equivalent operation; the dashboard must not be the
    easier route to the more destructive outcome.
    """
    deleted: list[str] = []
    monkeypatch.setattr(
        server, "_get_state_manager", lambda _app: _FleetStateManager(["sbx-1", "sbx-2"])
    )
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _sdk_where(deleted.append))

    response = asyncio.run(server.execute_command(server.CommandRequest(text="delete all")))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 409
    error = json.loads(response.body)["error"]
    assert "Deleting 2 sandboxes cannot be undone" in error
    assert "Run 'delete all --force' to confirm" in error
    assert deleted == [], "nothing may be deleted before the user confirms"


@pytest.mark.parametrize("target", ["all", "running"])
def test_execute_command_group_delete_proceeds_once_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    """--force is the confirmation, and it applies to any group selector."""
    deleted: list[str] = []
    monkeypatch.setattr(
        server, "_get_state_manager", lambda _app: _FleetStateManager(["sbx-1", "sbx-2"])
    )
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _sdk_where(deleted.append))

    response = asyncio.run(
        server.execute_command(server.CommandRequest(text=f"delete {target} --force"))
    )

    assert isinstance(response, server.CommandResponse)
    assert response.result == "Deleted 2 VMs."
    assert deleted == ["sbx-1", "sbx-2"]


def test_execute_command_single_delete_needs_no_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: one named sandbox matches the existing per-sandbox delete button."""
    deleted: list[str] = []
    monkeypatch.setattr(
        server, "_get_state_manager", lambda _app: _FleetStateManager(["sbx-1", "sbx-2"])
    )
    monkeypatch.setattr(server, "_get_sdk", lambda _app: _sdk_where(deleted.append))

    response = asyncio.run(server.execute_command(server.CommandRequest(text="delete sbx-1")))

    assert isinstance(response, server.CommandResponse)
    assert deleted == ["sbx-1"]


@pytest.mark.parametrize("verb", ["stop", "delete"])
def test_execute_command_unknown_sandbox_is_an_error_for_every_verb(
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
) -> None:
    """'stop'/'delete' on a name that does not exist is a failure, not "0 VMs".

    A 200 whose result reads "Stopped 0 VMs." renders in the dashboard's
    success area and clears the command, so a typo looks like it worked.
    """
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())
    monkeypatch.setattr(server, "_get_sdk", lambda _app: DummySDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text=f"{verb} sbx-nope")))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 404
    assert "No sandbox named 'sbx-nope'" in json.loads(response.body)["error"]


@pytest.mark.parametrize("text", ["stop all", "delete all", "stop error", "stop running"])
def test_execute_command_group_target_matching_nothing_is_a_result(
    monkeypatch: pytest.MonkeyPatch,
    text: str,
) -> None:
    """Control: a group that matches no sandbox is a true result, not an error."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())
    monkeypatch.setattr(server, "_get_sdk", lambda _app: DummySDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text=text)))

    assert isinstance(response, server.CommandResponse)
    assert response.affected_vms == []
    assert response.result.endswith("0 VMs.")


def test_execute_command_route_declares_its_error_responses() -> None:
    """The failure statuses the endpoint really returns must be in the schema.

    A caller reading only the schema would otherwise plan for 200 and 422 and
    be surprised by the 400 and 404 the command bar relies on.
    """
    route = next(
        r
        for r in server.app.routes
        if isinstance(r, APIRoute) and r.path == "/api/command" and "POST" in r.methods
    )

    # Every non-2xx the handler can actually return, so a caller reading only
    # the schema is not surprised by one of them.
    for status in (400, 404, 409, 500):
        assert route.responses[status]["model"] is server.CommandErrorResponse


def test_execute_command_returns_pydantic_model_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful command execution should return a validated CommandResponse."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())
    monkeypatch.setattr(server, "_get_sdk", lambda _app: DummySDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text="list")))

    assert isinstance(response, server.CommandResponse)
    assert response.action == "list"
    assert response.result == "Found 0 VMs."
    assert response.affected_vms == []


def test_execute_command_unknown_input_returns_400_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown command input should still return a 400 JSON error payload."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())
    monkeypatch.setattr(server, "_get_sdk", lambda _app: DummySDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text="nope")))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    # A user-facing error must name a recovery, not only state the problem.
    error = json.loads(response.body)["error"]
    assert "'nope' is not a command" in error
    # Bare verbs would pass even if the syntax were dropped; assert the forms.
    assert "'list'" in error
    for form in ("'info <sandbox>'", "'stop <sandbox>'", "'delete <sandbox>'"):
        assert form in error


def test_execute_command_unknown_status_names_the_valid_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bad 'list <status>' must tell the user which statuses exist."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())
    monkeypatch.setattr(server, "_get_sdk", lambda _app: DummySDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text="list bogus")))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    error = json.loads(response.body)["error"]
    assert "'bogus' is not a sandbox status" in error
    assert "Try 'list' on its own, or 'list' with one of:" in error
    for state in VMState:
        assert state.value in error


def test_execute_command_missing_vm_is_an_error_not_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'info <unknown>' failed, so it must not come back as a command result.

    A 200 with the text in ``result`` would render in the dashboard's success
    area and clear the command the user typed, as if the lookup had worked.
    """
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: DummyStateManager())
    monkeypatch.setattr(server, "_get_sdk", lambda _app: DummySDK())

    response = asyncio.run(server.execute_command(server.CommandRequest(text="info sbx-nope")))

    assert isinstance(response, JSONResponse)
    assert response.status_code == 404
    error = json.loads(response.body)["error"]
    assert "No sandbox named 'sbx-nope'" in error
    assert "Run 'list' to see the ones you have." in error


def test_open_vm_desktop_keeps_password_on_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from smolvm.types import DesktopEndpoint

    bundle = tmp_path / "mac-test"
    bundle.mkdir()
    (bundle / ".smolvm-vnc-password").write_text("private-secret\n")
    vm = SimpleNamespace(
        display=DesktopEndpoint(port=5901),
        config=SimpleNamespace(macos_machine=SimpleNamespace(bundle_path=bundle)),
    )
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub(vm))
    opened: list[tuple[DesktopEndpoint, str | None]] = []
    monkeypatch.setattr(
        "smolvm.macos.desktop.open_desktop",
        lambda endpoint, *, password=None: opened.append((endpoint, password)),
    )

    result = asyncio.run(server.open_vm_desktop("mac-test"))

    assert result == {"status": "opened", "vm_id": "mac-test"}
    assert opened == [(DesktopEndpoint(port=5901), "private-secret")]


def test_open_vm_desktop_not_found_names_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.open_vm_desktop("missing-mac"))

    assert exc_info.value.status_code == 404
    assert "Sandbox 'missing-mac'" in exc_info.value.detail
    assert "smolvm sandbox list --all" in exc_info.value.detail


# ── Tests for GET /api/vms/{vm_id}/processes ──


class _DummyNetwork:
    guest_ip = "172.16.0.2"
    gateway_ip = "172.16.0.1"
    tap_device = "tap0"
    ssh_host_port = 2201


class _DummyVMInfo:
    vm_id = "vm-test01"
    pid = 1234

    def __init__(self, *, status: object, network: object = None) -> None:
        self.status = status
        self.network = network


class _VMStateManagerStub:
    """StateManager stub that returns a configurable VMInfo for get_vm."""

    def __init__(self, vm: object | None = None) -> None:
        self._vm = vm

    def get_vm(self, vm_id: str) -> object:
        if self._vm is None:
            from smolvm.exceptions import VMNotFoundError

            raise VMNotFoundError(vm_id)
        return self._vm

    def list_vms(self, status: object = None) -> list[object]:
        return [self._vm] if self._vm else []


def test_get_vm_processes_vm_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should return 404 for unknown VM IDs."""
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_vm_processes("vm-nonexistent"))

    assert exc_info.value.status_code == 404


def test_get_vm_processes_vm_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should return 409 when the VM is stopped."""
    from smolvm.types import VMState

    vm = _DummyVMInfo(status=VMState.STOPPED, network=_DummyNetwork())
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub(vm))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_vm_processes("vm-test01"))

    assert exc_info.value.status_code == 409
    assert "not running" in exc_info.value.detail


def test_get_vm_processes_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should return 409 when the VM has no network."""
    from smolvm.types import VMState

    vm = _DummyVMInfo(status=VMState.RUNNING, network=None)
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub(vm))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(server.get_vm_processes("vm-test01"))

    assert exc_info.value.status_code == 409
    assert "no network" in exc_info.value.detail


def test_get_vm_processes_parses_ps_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should parse structured ps output correctly."""
    from unittest.mock import MagicMock, patch

    from smolvm.types import VMState

    vm = _DummyVMInfo(status=VMState.RUNNING, network=_DummyNetwork())
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub(vm))
    monkeypatch.setattr(server, "_resolve_ssh_key_path", lambda: None)

    ps_output = (
        "  PID USER       VSZ STAT COMMAND\n"
        "    1 root      1636 S    /sbin/init\n"
        "   42 root      1508 S    /usr/sbin/sshd\n"
        "  100 root      1440 R    ps -eo pid,user,vsz,stat,args\n"
    )

    mock_ssh = MagicMock()
    mock_ssh.run.return_value = MagicMock(exit_code=0, stdout=ps_output, stderr="")

    with patch("smolvm.ssh.SSHClient", return_value=mock_ssh):
        result = asyncio.run(server.get_vm_processes("vm-test01"))

    assert result["vm_id"] == "vm-test01"
    assert len(result["processes"]) == 3
    assert result["processes"][0]["pid"] == "1"
    assert result["processes"][0]["user"] == "root"
    assert result["processes"][0]["command"] == "/sbin/init"
    assert result["processes"][1]["pid"] == "42"
    assert result["processes"][1]["stat"] == "S"


def test_get_vm_processes_ssh_failure_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process endpoint should return 502 when SSH connection fails."""
    from unittest.mock import patch

    from smolvm.exceptions import SmolVMError
    from smolvm.types import VMState

    vm = _DummyVMInfo(status=VMState.RUNNING, network=_DummyNetwork())
    monkeypatch.setattr(server, "_get_state_manager", lambda _app: _VMStateManagerStub(vm))
    monkeypatch.setattr(server, "_resolve_ssh_key_path", lambda: None)

    with (
        patch("smolvm.ssh.SSHClient", side_effect=SmolVMError("Connection refused")),
        pytest.raises(HTTPException) as exc_info,
    ):
        asyncio.run(server.get_vm_processes("vm-test01"))

    assert exc_info.value.status_code == 502


def test_resolve_ssh_key_path_returns_none_when_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_resolve_ssh_key_path should return None when no key files exist."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert server._resolve_ssh_key_path() is None


def test_resolve_ssh_key_path_finds_keys_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """_resolve_ssh_key_path should find keys in ~/.smolvm/keys/."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    key_path = tmp_path / ".smolvm" / "keys" / "id_ed25519"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("dummy-key")

    assert server._resolve_ssh_key_path() == str(key_path)
