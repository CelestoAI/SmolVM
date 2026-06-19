"""Small Click helpers shared by SmolVM commands."""

from __future__ import annotations

import platform
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any, TypeVar

import click

F = TypeVar("F", bound=Callable[..., Any])

BACKENDS = ["auto", "firecracker", "qemu", "libkrun"]
QEMU_MACHINES = ["auto", "q35", "microvm"]
COMM_CHANNELS = ["ssh", "vsock"]


class LinuxOnlyOption(click.Option):
    """Hide and reject Linux-only options on non-Linux hosts."""

    def get_help_record(self, ctx: click.Context) -> tuple[str, str] | None:
        if platform.system() != "Linux":
            return None
        return super().get_help_record(ctx)

    def handle_parse_result(
        self,
        ctx: click.Context,
        opts: Mapping[str, Any],
        args: list[str],
    ) -> tuple[Any, list[str]]:
        if self.name in opts and platform.system() != "Linux":
            option = next(iter(self.opts), f"--{self.name}")
            raise click.UsageError(
                f"{option} is only supported on Linux. Run 'smolvm setup' without this flag."
            )
        return super().handle_parse_result(ctx, opts, args)


def _positive_number(
    ctx: click.Context,
    param: click.Parameter,
    value: float | int | None,
) -> float | int | None:
    if value is not None and value <= 0:
        raise click.BadParameter("value must be > 0", ctx=ctx, param=param)
    return value


def json_option(fn: F) -> F:
    return click.option("--json", "json_output", is_flag=True, help="Output a JSON envelope.")(fn)


def backend_option(*, default: str | None = None) -> Callable[[F], F]:
    return click.option(
        "--backend",
        type=click.Choice(BACKENDS),
        default=default,
        help="Virtualization backend.",
    )


def qemu_machine_option(fn: F) -> F:
    return click.option(
        "--qemu-machine",
        type=click.Choice(QEMU_MACHINES),
        default="auto",
        show_default=True,
        help="QEMU machine model.",
    )(fn)


def comm_channel_option(fn: F) -> F:
    return click.option(
        "--comm-channel",
        type=click.Choice(COMM_CHANNELS),
        default=None,
        help="Host-to-guest control channel.",
    )(fn)


def boot_timeout_option(fn: F) -> F:
    return click.option(
        "--boot-timeout",
        type=float,
        callback=_positive_number,
        default=30.0,
        show_default=True,
        help="Seconds to wait for the sandbox to be ready.",
    )(fn)


def ssh_auth_options(fn: F) -> F:
    @click.option("--ssh-key", default=None, help="Path to SSH private key.")
    @click.option("--ssh-user", default="root", show_default=True, help="SSH user.")
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]


def positive_int_type() -> click.IntRange:
    return click.IntRange(min=1)


def positive_float_type() -> click.FloatRange:
    return click.FloatRange(min=0, min_open=True)
