"""Click command tree for the SmolVM CLI."""

from __future__ import annotations

import importlib.metadata
import shlex
from types import SimpleNamespace
from typing import Any, cast

import click

from smolvm.cli.commands.options import (
    LinuxOnlyOption,
    backend_option,
    boot_timeout_option,
    comm_channel_option,
    complete_browser_session_names,
    complete_sandbox_names,
    image_dir_option,
    json_option,
    positive_float_type,
    positive_int_type,
    qemu_machine_option,
    ssh_auth_options,
)
from smolvm.cli.version_check import maybe_print_update_notice
from smolvm.host.doctor import run_doctor
from smolvm.types import BrowserSessionState, GuestFlushPolicy, GuestOS, SnapshotType, VMState

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _handlers() -> Any:
    from smolvm.cli import main

    return main


def _before_command(*, json_output: bool = False, skip_update_notice: bool = False) -> None:
    maybe_print_update_notice(json_output=json_output or skip_update_notice)


def _ns(**values: Any) -> SimpleNamespace:
    return SimpleNamespace(**values)


def _mounts(values: tuple[str, ...]) -> list[str] | None:
    return list(values) or None


@click.group(context_settings=CONTEXT_SETTINGS, invoke_without_command=True)
@click.version_option(
    importlib.metadata.version("smolvm"),
    "-V",
    "--version",
    prog_name="smolvm",
    message="%(prog)s %(version)s",
)
@click.pass_context
def cli(ctx: click.Context) -> int | None:
    """Create, manage, and connect to disposable sandboxes for AI agents."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())
        return 0
    return None


@cli.group(context_settings=CONTEXT_SETTINGS)
def sandbox() -> None:
    """Create, inspect, connect to, and delete sandboxes."""


@sandbox.command("create")
@click.option("-n", "--name", default=None, help="Name for the sandbox.")
@click.option(
    "--os",
    "os_name",
    type=click.Choice([guest_os.value for guest_os in GuestOS]),
    default=None,
    help="Operating system image; auto-detected when omitted.",
)
@click.option("--image", default=None, help="S3 URI or local qcow2 image path.")
@click.option("--memory", "memory_mib", type=int, default=None, metavar="MIB")
@click.option("--disk-size", "disk_size_mib", type=int, default=None, metavar="MIB")
@backend_option(default=None)
@qemu_machine_option
@comm_channel_option
@click.option("--mount", "mounts", multiple=True, metavar="HOST_PATH[:GUEST_PATH]")
@click.option("--writable-mounts", is_flag=True, help="Allow writes to mounted host folders.")
@click.option("--yes", is_flag=True, help="Confirm one-time macOS image preparation.")
@click.option(
    "--network",
    "network_mode",
    type=click.Choice(["nat", "bridge"]),
    default="nat",
    help="Network mode: 'nat' (default, private network) or 'bridge' (connect to a host bridge).",
)
@click.option(
    "--bridge",
    "bridge_name",
    default=None,
    metavar="BRIDGE",
    help="Linux bridge to attach to (required with --network bridge).",
)
@boot_timeout_option
@json_option
def sandbox_create(
    name: str | None,
    os_name: str | None,
    image: str | None,
    memory_mib: int | None,
    disk_size_mib: int | None,
    backend: str | None,
    qemu_machine: str,
    comm_channel: str | None,
    mounts: tuple[str, ...],
    writable_mounts: bool,
    yes: bool,
    network_mode: str,
    bridge_name: str | None,
    boot_timeout: float,
    json_output: bool,
) -> Any:
    """Create a new sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_create(
        _ns(
            command_name="sandbox.create",
            name=name,
            os=os_name,
            image=image,
            memory_mib=memory_mib,
            disk_size_mib=disk_size_mib,
            backend=backend,
            qemu_machine=qemu_machine,
            comm_channel=comm_channel,
            mounts=_mounts(mounts),
            writable_mounts=writable_mounts,
            yes=yes,
            network_mode=network_mode,
            bridge_name=bridge_name,
            boot_timeout=boot_timeout,
            json=json_output,
        )
    )


@sandbox.command("list")
@click.option("--all", "include_all", is_flag=True, help="Show all sandboxes.")
@click.option(
    "--status",
    "status_filter",
    type=click.Choice([state.value for state in VMState]),
    default=None,
)
@json_option
def sandbox_list(include_all: bool, status_filter: str | None, json_output: bool) -> Any:
    """List your sandboxes."""
    if include_all and status_filter is not None:
        raise click.UsageError(
            "Use one filter. Run 'smolvm sandbox list --all' or "
            "'smolvm sandbox list --status running'."
        )
    _before_command(json_output=json_output)
    return _handlers()._run_list(
        include_all=include_all,
        status_filter=status_filter,
        json_output=json_output,
        command_name="sandbox.list",
    )


@sandbox.command("info")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@json_option
def sandbox_info(vm_id: str, json_output: bool) -> Any:
    """Show details about a sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_info(
        vm_id=vm_id,
        json_output=json_output,
        command_name="sandbox.info",
    )


@sandbox.command("start")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@boot_timeout_option
@json_option
def sandbox_start(vm_id: str, boot_timeout: float, json_output: bool) -> Any:
    """Start a stopped sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_vm_start(
        _ns(command_name="sandbox.start", vm_id=vm_id, boot_timeout=boot_timeout, json=json_output)
    )


@sandbox.command("desktop")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.option("--start", "start_if_needed", is_flag=True, help="Start a stopped sandbox first.")
@boot_timeout_option
@json_option
def sandbox_desktop(
    vm_id: str,
    start_if_needed: bool,
    boot_timeout: float,
    json_output: bool,
) -> Any:
    """Open a sandbox desktop."""
    _before_command(json_output=json_output)
    return _handlers()._run_desktop(
        _ns(
            command_name="sandbox.desktop",
            vm_id=vm_id,
            start=start_if_needed,
            boot_timeout=boot_timeout,
            json=json_output,
        )
    )


@sandbox.command("stop")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.option(
    "--timeout",
    type=positive_float_type(),
    default=3.0,
    show_default=True,
    help="Seconds to wait before forcing shutdown.",
)
@json_option
def sandbox_stop(vm_id: str, timeout: float, json_output: bool) -> Any:
    """Stop a running sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_stop(
        _ns(command_name="sandbox.stop", vm_id=vm_id, timeout=timeout, json=json_output)
    )


@sandbox.command("pause")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@json_option
def sandbox_pause(vm_id: str, json_output: bool) -> Any:
    """Pause a running sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_pause(_ns(command_name="sandbox.pause", vm_id=vm_id, json=json_output))


@sandbox.command("resume")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@json_option
def sandbox_resume(vm_id: str, json_output: bool) -> Any:
    """Resume a paused sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_resume(
        _ns(command_name="sandbox.resume", vm_id=vm_id, json=json_output)
    )


@sandbox.command("shell")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@boot_timeout_option
def sandbox_shell(vm_id: str, boot_timeout: float) -> Any:
    """Open a fast shell in a sandbox."""
    _before_command()
    return _handlers()._run_shell(
        _ns(
            command_name="sandbox.shell",
            vm_id=vm_id,
            boot_timeout=boot_timeout,
        )
    )


@sandbox.command("ssh")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@ssh_auth_options
@boot_timeout_option
def sandbox_ssh(vm_id: str, ssh_key: str | None, ssh_user: str, boot_timeout: float) -> Any:
    """Open an SSH shell in a sandbox."""
    _before_command()
    return _handlers()._run_ssh(
        _ns(
            command_name="sandbox.ssh",
            vm_id=vm_id,
            ssh_key=ssh_key,
            ssh_user=ssh_user,
            boot_timeout=boot_timeout,
        )
    )


@sandbox.command(
    "exec",
    short_help="Run a command in a sandbox and print its output.",
    context_settings={"ignore_unknown_options": True},
)
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.argument("command", nargs=-1, required=True, metavar="-- COMMAND ...")
@click.option(
    "--timeout",
    type=positive_int_type(),
    default=30,
    show_default=True,
    help="Seconds to wait for the command to finish.",
)
@click.option(
    "--start",
    is_flag=True,
    help="Start the sandbox first if it is not already running.",
)
@boot_timeout_option
@json_option
def sandbox_exec(
    vm_id: str,
    command: tuple[str, ...],
    timeout: int,
    start: bool,
    boot_timeout: float,
    json_output: bool,
) -> Any:
    """Run a command in a sandbox and print its output.

    The sandbox must already be running. Pass --start to have it started
    automatically first.

    \b
    Example:
      smolvm sandbox exec my-sandbox -- ls -la /root
    """
    _before_command(json_output=json_output)
    return _handlers()._run_exec(
        _ns(
            command_name="sandbox.exec",
            vm_id=vm_id,
            command=command,
            timeout=timeout,
            start=start,
            boot_timeout=boot_timeout,
            json=json_output,
        )
    )


@sandbox.command("logs")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.option(
    "--tail",
    type=positive_int_type(),
    default=200,
    show_default=True,
    help="Number of lines to show from the end of the log.",
)
@click.option("-f", "--follow", is_flag=True, help="Keep printing new log lines as they arrive.")
@json_option
def sandbox_logs(vm_id: str, tail: int, follow: bool, json_output: bool) -> Any:
    """Show a sandbox's boot and console logs."""
    _before_command(json_output=json_output)
    return _handlers()._run_logs(
        _ns(
            command_name="sandbox.logs",
            vm_id=vm_id,
            tail=tail,
            follow=follow,
            json=json_output,
        )
    )


@sandbox.command("delete")
@click.argument("vm_ids", nargs=-1, metavar="sandbox...", shell_complete=complete_sandbox_names)
@click.option("--all", "all_sandboxes", is_flag=True, help="Delete every sandbox.")
@click.option("--force", is_flag=True, help="Skip the confirmation prompt for --all.")
@click.option("--dry-run", is_flag=True, help="Show what would be deleted.")
@json_option
def sandbox_delete(
    vm_ids: tuple[str, ...],
    all_sandboxes: bool,
    force: bool,
    dry_run: bool,
    json_output: bool,
) -> Any:
    """Delete one or more sandboxes."""
    if all_sandboxes and vm_ids:
        raise click.UsageError(
            "Choose one target. Run 'smolvm sandbox delete my-sandbox' or "
            "'smolvm sandbox delete --all --force'."
        )
    if not all_sandboxes and not vm_ids:
        raise click.UsageError(
            "Pass a target. Run 'smolvm sandbox delete my-sandbox' or "
            "'smolvm sandbox delete --all --force'."
        )
    if all_sandboxes and json_output and not force:
        from smolvm.cli.output import emit_error

        return emit_error(
            "sandbox.delete",
            "refused",
            "Refusing to delete sandboxes without --force in --json mode. "
            "Run 'smolvm sandbox delete --all --force --json' to confirm.",
            recovery="Run 'smolvm sandbox delete --all --force --json' to confirm.",
        )

    _before_command(json_output=json_output)
    from smolvm.cli.cleanup import run_cleanup, run_delete

    if all_sandboxes:
        return run_cleanup(
            dry_run=dry_run,
            json_output=json_output,
            force=force,
            command_name="sandbox.delete",
        )
    return run_delete(
        vm_ids=list(vm_ids),
        dry_run=dry_run,
        json_output=json_output,
        command_name="sandbox.delete",
    )


@cli.command("setup", help="Install or validate local runtime dependencies.")
@click.option("--check-only", is_flag=True, help="Check what is needed without installing.")
@click.option("--with-docker", is_flag=True, help="Also install or check Docker.")
@click.option("--macos", "macos_runtime", is_flag=True, help="Prepare macOS desktop sandboxes.")
@click.option("--skip-deps", is_flag=True, help="Skip installing system packages.")
@click.option("--assets-dir", is_flag=True, help="Print the packaged setup-assets directory.")
@click.option("--no-configure-runtime", cls=LinuxOnlyOption, is_flag=True)
@click.option("--runtime-user", cls=LinuxOnlyOption, default=None)
@click.option("--remove-runtime-config", cls=LinuxOnlyOption, is_flag=True)
@click.option("--for-bake", cls=LinuxOnlyOption, is_flag=True)
@click.option("--skip-kvm-check", cls=LinuxOnlyOption, is_flag=True)
@click.option("--skip-runtime-check", cls=LinuxOnlyOption, is_flag=True)
@click.option("--firecracker-version", cls=LinuxOnlyOption, default=None, metavar="VER")
def setup(
    check_only: bool,
    with_docker: bool,
    macos_runtime: bool,
    skip_deps: bool,
    assets_dir: bool,
    no_configure_runtime: bool,
    runtime_user: str | None,
    remove_runtime_config: bool,
    for_bake: bool,
    skip_kvm_check: bool,
    skip_runtime_check: bool,
    firecracker_version: str | None,
) -> Any:
    _before_command(skip_update_notice=assets_dir)
    return _handlers()._run_setup(
        check_only=check_only,
        with_docker=with_docker,
        macos_runtime=macos_runtime,
        configure_runtime=not no_configure_runtime,
        no_configure_runtime=no_configure_runtime,
        skip_deps=skip_deps,
        runtime_user=runtime_user,
        remove_runtime_config=remove_runtime_config,
        for_bake=for_bake,
        skip_kvm_check=skip_kvm_check,
        skip_runtime_check=skip_runtime_check,
        firecracker_version=firecracker_version,
        assets_dir=assets_dir,
    )


@cli.command("doctor", help="Check whether this machine can run sandboxes.")
@backend_option(default=None)
@click.option("--strict", is_flag=True, help="Fail if any check reports a warning.")
@json_option
def doctor(backend: str | None, strict: bool, json_output: bool) -> Any:
    _before_command(json_output=json_output)
    return run_doctor(backend=backend, json_output=json_output, strict=strict)


@cli.command("update", help="Upgrade SmolVM to the latest stable release.")
@click.option("--check", "check_only", is_flag=True, help="Check without installing.")
@json_option
def update(check_only: bool, json_output: bool) -> Any:
    _before_command(json_output=json_output)
    from smolvm.cli.update import run_update

    return run_update(check=check_only, json_output=json_output)


@cli.command("completion", short_help="Print or install a shell tab-completion script.")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
@click.option(
    "--install",
    "install",
    is_flag=True,
    help="Set up completion for the shell in one step instead of printing the script.",
)
def completion(shell: str, install: bool) -> int:
    """Print or install a shell tab-completion script for smolvm.

    Completion suggests subcommands, options, and the names of your existing
    sandboxes. The quickest setup is one command:

    \b
      smolvm completion bash --install   # also: zsh, fish

    Without --install the script is printed so you can wire it up yourself:

    \b
    bash:  eval "$(smolvm completion bash)"
    zsh:   eval "$(smolvm completion zsh)"
    fish:  smolvm completion fish > ~/.config/fish/completions/smolvm.fish
    """
    from click.shell_completion import get_completion_class

    completion_cls = get_completion_class(shell)
    if completion_cls is None:  # pragma: no cover - guarded by click.Choice
        raise click.UsageError(f"Shell completion is not supported for '{shell}'.")
    comp = completion_cls(cli, {}, "smolvm", "_SMOLVM_COMPLETE")
    script = comp.source()
    if install:
        from smolvm.cli.completion import run_completion_install

        return run_completion_install(shell, script)
    click.echo(script)
    return 0


@cli.command("prune", help="Remove stale image-cache entries (alias for 'smolvm image prune').")
@click.option("--dry-run", is_flag=True, help="Show what would be removed.")
@click.option("--cache-dir", default=None, hidden=True)
@image_dir_option
@json_option
def prune(dry_run: bool, cache_dir: str | None, image_dir: str | None, json_output: bool) -> Any:
    _before_command(json_output=json_output)
    from smolvm.cli.prune import run_prune

    return run_prune(dry_run=dry_run, json_output=json_output, cache_dir=image_dir or cache_dir)


@cli.group(context_settings=CONTEXT_SETTINGS)
def image() -> None:
    """Download and manage cached sandbox images."""


@image.command("pull")
@click.argument("preset", metavar="[preset]", required=False, default=None)
@click.option(
    "--all",
    "all_images",
    is_flag=True,
    help="Download every image for this machine (all operating systems).",
)
@click.option(
    "--arch",
    type=click.Choice(["amd64", "arm64"]),
    default=None,
    help="Processor type of the image; defaults to this machine's.",
)
@click.option(
    "--vmm",
    type=click.Choice(["firecracker", "qemu", "libkrun"]),
    default=None,
    help="Virtual machine engine that boots the image; defaults to what this machine uses.",
)
@click.option(
    "--os",
    "os_name",
    type=click.Choice(["ubuntu", "alpine"]),
    default=None,
    help="Operating system inside the image; single pulls default to ubuntu, "
    "and with --all this limits the download.",
)
@image_dir_option
@json_option
def image_pull(
    preset: str | None,
    all_images: bool,
    arch: str | None,
    vmm: str | None,
    os_name: str | None,
    image_dir: str | None,
    json_output: bool,
) -> Any:
    """Download a sandbox image before first use."""
    if bool(preset) == all_images:
        raise click.UsageError(
            "Choose one target: 'smolvm image pull codex' or 'smolvm image pull --all'."
        )
    _before_command(json_output=json_output)
    if all_images:
        from smolvm.cli.image import run_image_pull_all

        return run_image_pull_all(
            arch=arch,
            vmm=vmm,
            os_name=os_name,
            image_dir=image_dir,
            json_output=json_output,
        )
    from smolvm.cli.image import run_image_pull

    return run_image_pull(
        preset=cast(str, preset),
        arch=arch,
        vmm=vmm,
        os_name=os_name,
        image_dir=image_dir,
        json_output=json_output,
    )


@image.command("list")
@image_dir_option
@json_option
def image_list(image_dir: str | None, json_output: bool) -> Any:
    """Show downloaded images and how much space they use."""
    _before_command(json_output=json_output)
    from smolvm.cli.image import run_image_list

    return run_image_list(image_dir=image_dir, json_output=json_output)


# Docker-familiar spelling; same command, same "image.list" JSON envelope.
image.add_command(image_list, name="ls")


@cli.command("images", help="Show downloaded images (alias for 'smolvm image list').")
@image_dir_option
@json_option
def images(image_dir: str | None, json_output: bool) -> Any:
    _before_command(json_output=json_output)
    from smolvm.cli.image import run_image_list

    return run_image_list(image_dir=image_dir, json_output=json_output, command_name="images")


@image.command("inspect")
@click.argument("name", metavar="name-or-preset")
@image_dir_option
@json_option
def image_inspect(name: str, image_dir: str | None, json_output: bool) -> Any:
    """Show one image in detail: files, checksums, and where it came from."""
    _before_command(json_output=json_output)
    from smolvm.cli.image import run_image_inspect

    return run_image_inspect(name=name, image_dir=image_dir, json_output=json_output)


@image.command("rm")
@click.argument("name", metavar="name-or-preset")
@click.option("--dry-run", is_flag=True, help="Show what would be removed.")
@image_dir_option
@json_option
def image_rm(name: str, dry_run: bool, image_dir: str | None, json_output: bool) -> Any:
    """Remove a downloaded image to free disk space."""
    _before_command(json_output=json_output)
    from smolvm.cli.image import run_image_rm

    return run_image_rm(
        name=name,
        image_dir=image_dir,
        dry_run=dry_run,
        json_output=json_output,
    )


@image.command("build")
@click.argument("context_path", metavar="path", default=".")
@click.option("-t", "--tag", required=True, metavar="NAME", help="Name for the built image.")
@click.option(
    "--os",
    "os_name",
    type=click.Choice(["macos"]),
    default=None,
    help="Build a local macOS desktop image instead of a Dockerfile image.",
)
@click.option(
    "--ipsw",
    default=None,
    metavar="latest|PATH",
    help="Apple restore image for --os macos; defaults to latest.",
)
@click.option("-f", "--file", "dockerfile", default=None, metavar="PATH", help="Dockerfile path.")
@click.option(
    "--size-mb",
    type=positive_int_type(),
    default=512,
    show_default=True,
    help="Disk size of the built image.",
)
@click.option(
    "--build-arg",
    "build_args",
    multiple=True,
    metavar="KEY=VALUE",
    help="Values passed to the Dockerfile's ARG lines.",
)
@click.option(
    "--arch",
    type=click.Choice(["amd64", "arm64"]),
    default=None,
    help="Processor type to build for; defaults to this machine's.",
)
@backend_option(default="auto")
@click.option("--init", default="/init", show_default=True, help="Program the image starts with.")
@image_dir_option
@json_option
@click.pass_context
def image_build(
    ctx: click.Context,
    context_path: str,
    tag: str,
    os_name: str | None,
    ipsw: str | None,
    dockerfile: str | None,
    size_mb: int,
    build_args: tuple[str, ...],
    arch: str | None,
    backend: str,
    init: str,
    image_dir: str | None,
    json_output: bool,
) -> Any:
    """Build a Linux image from a Dockerfile or a local macOS image."""
    _before_command(json_output=json_output)
    if os_name == "macos":
        if dockerfile is not None or build_args or arch is not None or init != "/init":
            raise click.UsageError(
                "--file, --build-arg, --arch, and --init only apply to Dockerfile images."
            )
        unsupported = [
            option
            for option, parameter in (("--size-mb", "size_mb"), ("--backend", "backend"))
            if ctx.get_parameter_source(parameter) is click.core.ParameterSource.COMMANDLINE
        ]
        if unsupported:
            options = " and ".join(unsupported)
            retry_arguments = [
                "smolvm",
                "image",
                "build",
                "--os",
                "macos",
                "--ipsw",
                ipsw or "latest",
                "-t",
                tag,
            ]
            if image_dir is not None:
                retry_arguments.extend(["--image-dir", image_dir])
            if json_output:
                retry_arguments.append("--json")
            retry = shlex.join(retry_arguments)
            raise click.UsageError(
                f"{options} {'is' if len(unsupported) == 1 else 'are'} not available for macOS "
                f"images; run '{retry}' without {'it' if len(unsupported) == 1 else 'them'}."
            )
        from smolvm.cli.image import run_macos_image_build

        return run_macos_image_build(
            tag=tag,
            ipsw=ipsw or "latest",
            image_dir=image_dir,
            json_output=json_output,
        )
    if ipsw is not None:
        raise click.UsageError("--ipsw requires --os macos.")

    from smolvm.cli.image import run_image_build

    return run_image_build(
        tag=tag,
        context_path=context_path,
        dockerfile=dockerfile,
        size_mb=size_mb,
        build_args=build_args,
        arch=arch,
        backend=backend,
        init=init,
        image_dir=image_dir,
        json_output=json_output,
    )


@image.command("save")
@click.argument("name", metavar="name")
@click.option("-o", "--output", required=True, metavar="FILE", help="Where to write the archive.")
@image_dir_option
@json_option
def image_save(name: str, output: str, image_dir: str | None, json_output: bool) -> Any:
    """Pack a downloaded image into a file you can copy to another machine."""
    _before_command(json_output=json_output)
    from smolvm.cli.image_transfer import run_image_save

    return run_image_save(name=name, output=output, image_dir=image_dir, json_output=json_output)


@image.command("load")
@click.option("-i", "--input", "input_file", required=True, metavar="FILE", help="Archive to load.")
@click.option("--force", is_flag=True, help="Replace the image if it already exists.")
@image_dir_option
@json_option
def image_load(input_file: str, force: bool, image_dir: str | None, json_output: bool) -> Any:
    """Unpack an image archive created with 'smolvm image save'."""
    _before_command(json_output=json_output)
    from smolvm.cli.image_transfer import run_image_load

    return run_image_load(
        input_file=input_file,
        image_dir=image_dir,
        force=force,
        json_output=json_output,
    )


@image.command("prune", help="Remove image caches left behind by older SmolVM versions.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed.")
@image_dir_option
@json_option
def image_prune(dry_run: bool, image_dir: str | None, json_output: bool) -> Any:
    _before_command(json_output=json_output)
    from smolvm.cli.prune import run_prune

    return run_prune(
        dry_run=dry_run,
        json_output=json_output,
        cache_dir=image_dir,
        command_name="image.prune",
    )


@cli.command("ui", help="Start the local dashboard.")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8080, show_default=True, type=int)
@click.option("--allow-beta", is_flag=True)
def ui(host: str, port: int, allow_beta: bool) -> Any:
    _before_command()
    return _handlers()._run_ui(host=host, port=port, allow_beta=allow_beta)


@cli.group(context_settings=CONTEXT_SETTINGS)
def server() -> None:
    """Run the local SmolVM HTTP API."""


@server.command("start")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
def server_start(host: str, port: int) -> Any:
    """Start the local API server."""
    _before_command()
    return _handlers()._run_server_start(host=host, port=port)


@sandbox.group(context_settings=CONTEXT_SETTINGS)
def snapshot() -> None:
    """Save and restore sandbox state."""


@snapshot.command("create")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.option("--snapshot-id", default=None)
@click.option(
    "--snapshot-type",
    type=click.Choice([snapshot_type.value for snapshot_type in SnapshotType]),
    default=SnapshotType.FULL.value,
    show_default=True,
)
@click.option("--resume-source", is_flag=True)
@click.option(
    "--live-only",
    is_flag=True,
    help="Keep a running QEMU sandbox available during a disk snapshot, or fail.",
)
@click.option(
    "--flush-policy",
    type=click.Choice([policy.value for policy in GuestFlushPolicy]),
    default=GuestFlushPolicy.REQUIRED.value,
    show_default=True,
    help=(
        "How to save pending file writes before a disk snapshot: required fails on "
        "error, best-effort continues, and skip does not try."
    ),
)
@json_option
def snapshot_create(
    vm_id: str,
    snapshot_id: str | None,
    snapshot_type: str,
    resume_source: bool,
    live_only: bool,
    flush_policy: str,
    json_output: bool,
) -> Any:
    """Save a sandbox snapshot."""
    _before_command(json_output=json_output)
    return _handlers()._run_snapshot(
        _ns(
            snapshot_action="create",
            vm_id=vm_id,
            snapshot_id=snapshot_id,
            snapshot_type=snapshot_type,
            resume_source=resume_source,
            live_only=live_only,
            flush_policy=flush_policy,
            command_name="sandbox.snapshot.create",
            json=json_output,
        )
    )


@snapshot.command("restore")
@click.argument("snapshot_id", metavar="snapshot")
@click.option("--resume", is_flag=True)
@click.option("--force", is_flag=True)
@json_option
def snapshot_restore(snapshot_id: str, resume: bool, force: bool, json_output: bool) -> Any:
    """Restore a sandbox from a snapshot."""
    _before_command(json_output=json_output)
    return _handlers()._run_snapshot(
        _ns(
            snapshot_action="restore",
            snapshot_id=snapshot_id,
            resume=resume,
            force=force,
            command_name="sandbox.snapshot.restore",
            json=json_output,
        )
    )


@snapshot.command("delete")
@click.argument("snapshot_id", metavar="snapshot")
@json_option
def snapshot_delete(snapshot_id: str, json_output: bool) -> Any:
    """Delete a snapshot."""
    _before_command(json_output=json_output)
    return _handlers()._run_snapshot(
        _ns(
            snapshot_action="delete",
            snapshot_id=snapshot_id,
            command_name="sandbox.snapshot.delete",
            json=json_output,
        )
    )


@snapshot.command("list")
@click.option("--vm-id", default=None)
@json_option
def snapshot_list(vm_id: str | None, json_output: bool) -> Any:
    """List saved snapshots."""
    _before_command(json_output=json_output)
    return _handlers()._run_snapshot(
        _ns(
            snapshot_action="list",
            vm_id=vm_id,
            command_name="sandbox.snapshot.list",
            json=json_output,
        )
    )


@sandbox.group(context_settings=CONTEXT_SETTINGS)
def file() -> None:
    """Copy files into or out of a sandbox."""


@file.command("upload")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.argument("local_path", metavar="local-path")
@click.argument("guest_path", metavar="guest-path")
@click.option("--no-create-dirs", is_flag=True)
@ssh_auth_options
@comm_channel_option
@json_option
def file_upload(
    vm_id: str,
    local_path: str,
    guest_path: str,
    no_create_dirs: bool,
    ssh_key: str | None,
    ssh_user: str,
    comm_channel: str | None,
    json_output: bool,
) -> Any:
    """Copy a file into a sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_file(
        _ns(
            file_action="upload",
            vm_id=vm_id,
            local_path=local_path,
            guest_path=guest_path,
            no_create_dirs=no_create_dirs,
            ssh_key=ssh_key,
            ssh_user=ssh_user,
            comm_channel=comm_channel,
            command_name="sandbox.file.upload",
            json=json_output,
        )
    )


@file.command("download")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.argument("guest_path", metavar="guest-path")
@click.argument("local_path", metavar="local-path")
@click.option("--no-create-dirs", is_flag=True)
@ssh_auth_options
@comm_channel_option
@json_option
def file_download(
    vm_id: str,
    guest_path: str,
    local_path: str,
    no_create_dirs: bool,
    ssh_key: str | None,
    ssh_user: str,
    comm_channel: str | None,
    json_output: bool,
) -> Any:
    """Copy a file out of a sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_file(
        _ns(
            file_action="download",
            vm_id=vm_id,
            guest_path=guest_path,
            local_path=local_path,
            no_create_dirs=no_create_dirs,
            ssh_key=ssh_key,
            ssh_user=ssh_user,
            comm_channel=comm_channel,
            command_name="sandbox.file.download",
            json=json_output,
        )
    )


@sandbox.group(context_settings=CONTEXT_SETTINGS)
def env() -> None:
    """Manage sandbox environment variables."""


@env.command("set")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.argument("pairs", nargs=-1, required=True, metavar="KEY=VALUE...")
@ssh_auth_options
@comm_channel_option
@json_option
def env_set(
    vm_id: str,
    pairs: tuple[str, ...],
    ssh_key: str | None,
    ssh_user: str,
    comm_channel: str | None,
    json_output: bool,
) -> Any:
    """Set environment variables in a sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_env(
        _ns(
            env_action="set",
            vm_id=vm_id,
            pairs=list(pairs),
            ssh_key=ssh_key,
            ssh_user=ssh_user,
            comm_channel=comm_channel,
            command_name="sandbox.env.set",
            json=json_output,
        )
    )


@env.command("unset")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.argument("keys", nargs=-1, required=True, metavar="KEY...")
@ssh_auth_options
@comm_channel_option
@json_option
def env_unset(
    vm_id: str,
    keys: tuple[str, ...],
    ssh_key: str | None,
    ssh_user: str,
    comm_channel: str | None,
    json_output: bool,
) -> Any:
    """Remove environment variables from a sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_env(
        _ns(
            env_action="unset",
            vm_id=vm_id,
            keys=list(keys),
            ssh_key=ssh_key,
            ssh_user=ssh_user,
            comm_channel=comm_channel,
            command_name="sandbox.env.unset",
            json=json_output,
        )
    )


@env.command("list")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.option("--show-values", is_flag=True)
@ssh_auth_options
@comm_channel_option
@json_option
def env_list(
    vm_id: str,
    show_values: bool,
    ssh_key: str | None,
    ssh_user: str,
    comm_channel: str | None,
    json_output: bool,
) -> Any:
    """List environment variables in a sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_env(
        _ns(
            env_action="list",
            vm_id=vm_id,
            show_values=show_values,
            ssh_key=ssh_key,
            ssh_user=ssh_user,
            comm_channel=comm_channel,
            command_name="sandbox.env.list",
            json=json_output,
        )
    )


@sandbox.group(context_settings=CONTEXT_SETTINGS)
def port() -> None:
    """Manage port forwarding for a sandbox."""


@port.command("expose")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.argument("mapping", metavar="[host-port:]sandbox-port")
@ssh_auth_options
@comm_channel_option
@json_option
def port_expose(
    vm_id: str,
    mapping: str,
    ssh_key: str | None,
    ssh_user: str,
    comm_channel: str | None,
    json_output: bool,
) -> Any:
    """Share a sandbox port with your machine."""
    _before_command(json_output=json_output)
    return _handlers()._run_port_expose(
        _ns(
            vm_id=vm_id,
            mapping=mapping,
            ssh_key=ssh_key,
            ssh_user=ssh_user,
            comm_channel=comm_channel,
            command_name="sandbox.port.expose",
            json=json_output,
        )
    )


@port.command("close")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@click.argument("mapping", metavar="host-port:sandbox-port")
@ssh_auth_options
@comm_channel_option
@json_option
def port_close(
    vm_id: str,
    mapping: str,
    ssh_key: str | None,
    ssh_user: str,
    comm_channel: str | None,
    json_output: bool,
) -> Any:
    """Stop sharing a sandbox port."""
    _before_command(json_output=json_output)
    return _handlers()._run_port_close(
        _ns(
            vm_id=vm_id,
            mapping=mapping,
            ssh_key=ssh_key,
            ssh_user=ssh_user,
            comm_channel=comm_channel,
            command_name="sandbox.port.close",
            json=json_output,
        )
    )


@port.command("list")
@click.argument("vm_id", metavar="sandbox", shell_complete=complete_sandbox_names)
@ssh_auth_options
@comm_channel_option
@json_option
def port_list(
    vm_id: str,
    ssh_key: str | None,
    ssh_user: str,
    comm_channel: str | None,
    json_output: bool,
) -> Any:
    """List shared sandbox ports."""
    _before_command(json_output=json_output)
    return _handlers()._run_port_list(
        _ns(
            vm_id=vm_id,
            ssh_key=ssh_key,
            ssh_user=ssh_user,
            comm_channel=comm_channel,
            command_name="sandbox.port.list",
            json=json_output,
        )
    )


@cli.group(context_settings=CONTEXT_SETTINGS)
def bridge() -> None:
    """Check host bridges for bridged networking."""


@bridge.command("check")
@click.argument("bridge_name", metavar="BRIDGE")
@json_option
def bridge_check(bridge_name: str, json_output: bool) -> Any:
    """Check whether a bridge is ready for bridged networking."""
    _before_command(json_output=json_output)
    return _handlers()._run_bridge_check(
        _ns(
            command_name="bridge.check",
            bridge_name=bridge_name,
            json=json_output,
        )
    )


@cli.group(context_settings=CONTEXT_SETTINGS)
def windows() -> None:
    """Build Windows guest images."""


@windows.command("build-image")
@click.option("--iso", "windows_iso", required=True, metavar="PATH")
@click.option("--virtio-win-iso", "virtio_win_iso", required=True, metavar="PATH")
@click.option("--output", "output_qcow2", required=True, metavar="PATH")
@click.option("--username", default="smolvm", show_default=True)
@click.option("--password", default="smolvm", show_default=True)
@click.option("--hostname", default="smolvm-win", show_default=True)
@click.option("--edition", default="Windows 11 Pro", show_default=True)
@click.option("--disk-size", "disk_size_mib", type=positive_int_type(), default=64 * 1024)
@click.option("--build-timeout", "build_timeout_s", type=positive_float_type(), default=45 * 60)
@json_option
def windows_build_image(
    windows_iso: str,
    virtio_win_iso: str,
    output_qcow2: str,
    username: str,
    password: str,
    hostname: str,
    edition: str,
    disk_size_mib: int,
    build_timeout_s: float,
    json_output: bool,
) -> Any:
    """Build a Windows sandbox image."""
    _before_command(json_output=json_output)
    return _handlers()._run_windows_build_image(
        _ns(
            windows_iso=windows_iso,
            virtio_win_iso=virtio_win_iso,
            output_qcow2=output_qcow2,
            username=username,
            password=password,
            hostname=hostname,
            edition=edition,
            disk_size_mib=disk_size_mib,
            build_timeout_s=build_timeout_s,
            json=json_output,
        )
    )


@cli.group(context_settings=CONTEXT_SETTINGS)
def browser() -> None:
    """Manage browser sandboxes."""


@browser.command("start")
@click.option("--session-id", default=None)
@backend_option(default="auto")
@click.option("--live", is_flag=True)
@click.option("--profile-mode", type=click.Choice(["ephemeral", "persistent"]), default="ephemeral")
@click.option("--profile-id", default=None)
@click.option("--timeout-minutes", type=int, default=30, show_default=True)
@click.option("--viewport-width", type=int, default=1280, show_default=True)
@click.option("--viewport-height", type=int, default=720, show_default=True)
@click.option("--memory", "memory_mib", type=int, default=2048, show_default=True)
@click.option("--disk-size", "disk_size_mib", type=int, default=4096, show_default=True)
@click.option("--record-video", is_flag=True)
@click.option("--no-downloads", is_flag=True)
@boot_timeout_option
@json_option
def browser_start(
    session_id: str | None,
    backend: str,
    live: bool,
    profile_mode: str,
    profile_id: str | None,
    timeout_minutes: int,
    viewport_width: int,
    viewport_height: int,
    memory_mib: int,
    disk_size_mib: int,
    record_video: bool,
    no_downloads: bool,
    boot_timeout: float,
    json_output: bool,
) -> Any:
    """Start a browser sandbox."""
    _before_command(json_output=json_output)
    return _handlers()._run_browser(
        _ns(
            browser_action="start",
            session_id=session_id,
            backend=backend,
            live=live,
            profile_mode=profile_mode,
            profile_id=profile_id,
            timeout_minutes=timeout_minutes,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            memory_mib=memory_mib,
            disk_size_mib=disk_size_mib,
            record_video=record_video,
            no_downloads=no_downloads,
            boot_timeout=boot_timeout,
            json=json_output,
        )
    )


@browser.command("stop")
@click.argument(
    "session_id", required=False, metavar="session", shell_complete=complete_browser_session_names
)
@click.option("--all", "all_sessions", is_flag=True)
def browser_stop(session_id: str | None, all_sessions: bool) -> Any:
    """Stop a browser sandbox."""
    if all_sessions == (session_id is not None):
        raise click.UsageError(
            "Choose one target. Run 'smolvm browser stop browser-id' or "
            "'smolvm browser stop --all'."
        )
    _before_command()
    return _handlers()._run_browser(
        _ns(browser_action="stop", session_id=session_id, all=all_sessions)
    )


@browser.command("list")
@click.option(
    "--status",
    type=click.Choice([state.value for state in BrowserSessionState]),
    default=None,
)
@json_option
def browser_list(status: str | None, json_output: bool) -> Any:
    """List browser sandboxes."""
    _before_command(json_output=json_output)
    return _handlers()._run_browser(_ns(browser_action="list", status=status, json=json_output))


@browser.command("open")
@click.argument("session_id", metavar="session", shell_complete=complete_browser_session_names)
def browser_open(session_id: str) -> Any:
    """Open a browser view."""
    _before_command()
    return _handlers()._run_browser(_ns(browser_action="open", session_id=session_id))


@browser.command("logs")
@click.argument("session_id", metavar="session", shell_complete=complete_browser_session_names)
@click.option("--tail", type=int, default=100, show_default=True)
def browser_logs(session_id: str, tail: int) -> Any:
    """Show recent browser output."""
    _before_command()
    return _handlers()._run_browser(_ns(browser_action="logs", session_id=session_id, tail=tail))


def _register_preset_commands() -> None:
    from smolvm.presets import list_presets

    for preset in list_presets():
        public_name = "claude" if preset.name == "claude-code" else preset.name
        if public_name not in {"codex", "claude", "openclaw", "hermes", "pi"}:
            continue

        @click.group(public_name, context_settings=CONTEXT_SETTINGS, help=preset.summary)
        def preset_group() -> None:
            pass

        def _make_start_callback(preset_name: str, command_name: str) -> Any:
            @click.command("start", help="Start this agent in a sandbox.")
            @click.option("-n", "--name", default=None)
            @click.option("--memory", "memory_mib", type=int, default=None)
            @click.option("--disk-size", "disk_size_mib", type=int, default=None)
            @backend_option(default=None)
            @qemu_machine_option
            @click.option(
                "--os",
                "os_name",
                type=click.Choice([guest_os.value for guest_os in GuestOS]),
                default=None,
            )
            @click.option("--mount", "mounts", multiple=True, metavar="HOST_PATH[:GUEST_PATH]")
            @click.option("--writable-mounts", is_flag=True)
            @click.option("--install-timeout", type=positive_float_type(), default=600.0)
            @click.option("--attach/--no-attach", default=None)
            @comm_channel_option
            @boot_timeout_option
            @json_option
            def start(
                name: str | None,
                memory_mib: int | None,
                disk_size_mib: int | None,
                backend: str | None,
                qemu_machine: str,
                os_name: str | None,
                mounts: tuple[str, ...],
                writable_mounts: bool,
                install_timeout: float,
                attach: bool | None,
                comm_channel: str | None,
                boot_timeout: float,
                json_output: bool,
            ) -> Any:
                _before_command(json_output=json_output)
                return _handlers()._run_start(
                    _ns(
                        command_name=f"{command_name}.start",
                        preset_name=preset_name,
                        name=name,
                        memory_mib=memory_mib,
                        disk_size_mib=disk_size_mib,
                        backend=backend,
                        qemu_machine=qemu_machine,
                        os=os_name,
                        mounts=_mounts(mounts),
                        writable_mounts=writable_mounts,
                        install_timeout=install_timeout,
                        attach=attach,
                        comm_channel=comm_channel,
                        boot_timeout=boot_timeout,
                        json=json_output,
                    )
                )

            return start

        preset_group.add_command(_make_start_callback(preset.name, public_name))
        cli.add_command(preset_group)


_register_preset_commands()


def build_cli() -> click.Group:
    """Return the root Click command."""
    return cli
