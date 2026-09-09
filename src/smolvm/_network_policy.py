"""Validation shared by public constructors and the execution boundary."""

import sys
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from smolvm.exceptions import ValidationError
from smolvm.types import InternetSettings


def parse_network_policy(value: InternetSettings | dict[str, Any]) -> InternetSettings:
    try:
        # model_copy/model_construct can bypass even frozen model validation.
        return InternetSettings.model_validate(
            value.model_dump(warnings=False) if isinstance(value, InternetSettings) else value
        )
    except PydanticValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False)
        first = errors[0]
        field = ".".join(str(part) for part in first["loc"])
        message = first["msg"].removeprefix("Value error, ")
        raise ValidationError(
            f"Invalid internet_settings{'.' + field if field else ''}: {message}",
            {"field": "internet_settings", "errors": errors},
        ) from exc


def validate_network_policy_options(
    settings: InternetSettings,
    *,
    backend: str,
    guest_os: str,
    comm_channel: str | None = None,
    has_mounts: bool = False,
    has_forwards: bool = False,
    network_mode: str = "nat",
    qemu_network: str = "slirp",
) -> None:
    if settings.is_allow_all_domains:
        return
    recovery = (
        "Use a Linux guest on Linux with backend='firecracker', comm_channel='vsock', "
        "and default private networking."
    )
    if guest_os in {"windows", "macos"}:
        raise ValidationError(f"This guest does not support network restrictions. {recovery}")
    if network_mode != "nat":
        raise ValidationError(
            "Network restrictions require private networking; "
            "set network_attachment={'mode': 'nat'} on VMConfig."
        )
    if settings.has_explicit_restrictions:
        if backend == "qemu":
            if settings.mode == "restricted" and (sys.platform != "linux" or qemu_network != "tap"):
                raise ValidationError(
                    "QEMU address restrictions require Linux TAP networking; use "
                    "SmolVM.from_image(image, backend='qemu', network='tap', "
                    "internet_settings={'mode': 'restricted', 'allowed_cidrs': ['203.0.113.7']}) "
                    "on Linux (qemu_network='tap' on VMConfig)."
                )
            if sys.platform not in {"linux", "darwin"} or (
                qemu_network == "tap" and sys.platform != "linux"
            ):
                raise ValidationError(
                    "This QEMU network configuration is unsupported; use mode='off' with "
                    "network='slirp' on macOS or Linux (qemu_network='slirp' on VMConfig)."
                )
            if qemu_network == "tap" and has_forwards:
                raise ValidationError(
                    "QEMU TAP does not support port_forwards; omit it and connect to the "
                    "sandbox's IP, or call sandbox.expose_local(8080) after starting it."
                )
            return
        if backend != "firecracker" or sys.platform != "linux":
            raise ValidationError(f"This network mode requires Linux Firecracker. {recovery}")
        if has_mounts or has_forwards:
            raise ValidationError(
                "This network mode cannot use shared folders or exposed ports; "
                "omit mounts and port_forwards (workspace_mounts on VMConfig)."
            )
        if comm_channel == "ssh":
            raise ValidationError(
                "This network mode requires a direct command connection; set comm_channel='vsock'."
            )
    elif backend != "firecracker" and not (backend == "qemu" and qemu_network == "tap"):
        raise ValidationError(f"This configuration cannot enforce network restrictions. {recovery}")
