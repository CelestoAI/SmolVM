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

"""SmolVM image-cache management — pull, list, inspect, build, and remove.

Backs the ``smolvm image`` command group. Images normally download lazily
the first time a sandbox starts; ``image pull`` fetches them ahead of time
(offline prep, CI warm-up), ``image list`` shows what is cached,
``image inspect`` shows one image in detail, ``image build`` bakes a custom
image from a Dockerfile, and ``image rm`` frees disk space. All commands
operate on the directory returned by
:func:`smolvm.images.manager.resolve_image_dir`.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict, cast

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from typing_extensions import NotRequired

from smolvm import __version__
from smolvm.cli.output import (
    console_stdout,
    emit_error,
    emit_success,
    render_empty,
    render_error,
)
from smolvm.cli.prune import _VERSION_PATTERN, _format_bytes, _size_on_disk, _total_size
from smolvm.images.manager import resolve_image_dir

# Cache directory names produced by ``published.cache_name()``:
# ``{preset}-v{cli_version}-{arch}-{vmm}`` with a ``-alpine`` suffix for
# Alpine rootfs builds (Ubuntu carries no suffix for backward compat).
# The preset group is non-greedy so hyphenated presets (``claude-code``)
# parse correctly against the ``-v<version>-`` anchor; the version
# fragment is shared with prune so list/rm/prune agree on what counts as
# a versioned cache directory (a test round-trips it against cache_name()).
_IMAGE_DIR_NAME_RE = re.compile(
    rf"^(?P<preset>[a-z0-9-]+?)-v(?P<version>{_VERSION_PATTERN})"
    r"-(?P<arch>amd64|arm64)-(?P<vmm>firecracker|qemu|libkrun)(?:-(?P<os>alpine))?$"
)

# Base kernel dirs from ``published.ensure_base_kernel()``.
_KERNEL_DIR_NAME_RE = re.compile(
    rf"^base-kernel-v(?P<version>{_VERSION_PATTERN})-(?P<arch>amd64|arm64)$"
)

# One path segment of a cache-entry name. Deliberately mirrors the builder's
# _safe_cache_name charset; "." and ".." are rejected separately.
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

# Custom builds live under this subdirectory: custom/<name>/<fingerprint>/.
_CUSTOM_DIR = "custom"
_MACOS_DIR = "macos"
_MACOS_MANIFEST = "smolvm-manifest.json"


def _canonical_preset(name: str) -> str:
    """Map a public CLI alias (e.g. ``claude``) to its manifest preset name.

    Alias data lives on the preset definitions themselves so a new alias
    automatically works here without touching this module.
    """
    from smolvm.presets import list_presets

    for preset in list_presets():
        if name == preset.name or name in preset.aliases:
            return preset.name
    return name


def _created_iso(path: Path) -> str | None:
    """ISO 8601 UTC creation marker for a cache entry (its dir mtime)."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _relative_time(iso_ts: str | None, *, now: datetime | None = None) -> str:
    """Docker-style relative age ("2 hours ago") for an ISO timestamp."""
    if not iso_ts:
        return "-"
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "-"
    if then.tzinfo is None:
        # A naive timestamp can't be compared to the aware clock; degrade
        # like any other unusable input instead of raising.
        return "-"
    current = now if now is not None else datetime.now(timezone.utc)
    seconds = max(0, int((current - then).total_seconds()))
    for unit, span in (
        ("year", 31_536_000),
        ("month", 2_592_000),
        ("week", 604_800),
        ("day", 86_400),
        ("hour", 3_600),
        ("minute", 60),
    ):
        count = seconds // span
        if count:
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return f"{seconds} second{'s' if seconds != 1 else ''} ago"


def _valid_entry_name(requested: str) -> bool:
    """Whether *requested* is a well-formed cache-entry name.

    Plain entries are a single path segment — any real directory name works,
    since users may drop oddly named dirs into the cache. Custom builds are
    addressed as ``custom/<name>`` or ``custom/<name>/<fingerprint>`` with
    the builder's conservative charset. The bare namespace directory
    ``custom`` is NOT an entry: accepting it would let one rm wipe every
    build at once.
    """
    if not requested or "\\" in requested:
        return False
    segments = requested.split("/")
    if any(seg in {"", ".", ".."} for seg in segments):
        return False
    if len(segments) == 1:
        return segments[0] not in {_CUSTOM_DIR, _MACOS_DIR}
    valid_namespaced = (segments[0] == _CUSTOM_DIR and len(segments) in {2, 3}) or (
        segments[0] == _MACOS_DIR and len(segments) == 2
    )
    return valid_namespaced and all(
        _SAFE_SEGMENT_RE.match(seg) and not seg.startswith(".") for seg in segments[1:]
    )


class ImageRow(TypedDict):
    """One cached entry in the image directory."""

    name: str
    kind: str  # "image" | "kernel" | "custom" | "other"
    preset: str | None
    version: str | None
    arch: str | None
    vmm: str | None
    os: str | None
    current: bool | None  # matches this CLI version; None when unversioned
    created: str | None  # ISO 8601 UTC, from the entry dir's mtime
    size_bytes: int
    path: str


class ImageListPayload(TypedDict):
    image_dir: str
    images: list[ImageRow]
    total_size_bytes: int


class ImagePullPayload(TypedDict):
    preset: str
    arch: str
    vmm: str
    os: str
    version: str
    name: str
    kernel_path: str
    rootfs_path: str
    size_bytes: int
    already_cached: bool
    warnings: list[str]


class PullFailure(TypedDict):
    preset: str
    os: str
    error: str


class ImagePullAllPayload(TypedDict):
    arch: str
    vmm: str
    pulled: list[ImagePullPayload]
    failed: list[PullFailure]
    warnings: list[str]


class ImageFileEntry(TypedDict):
    name: str
    size_bytes: int  # apparent size
    size_on_disk_bytes: int  # allocated blocks, sparse-aware


class ImageManifestInfo(TypedDict):
    kernel_url: str
    kernel_sha256: str
    rootfs_url: str
    rootfs_sha256: str
    images_release_tag: str


class MacOSImageInfo(TypedDict):
    guest_version: str
    source_ipsw: str
    logical_size_bytes: int
    allocated_size_bytes: int
    driver_version: str


class ImageInspectEntry(ImageRow):
    files: list[ImageFileEntry]
    rootfs_sidecar: str | None
    manifest: ImageManifestInfo | None
    macos: NotRequired[MacOSImageInfo]


class ImageBuildPayload(TypedDict):
    name: str
    fingerprint: str
    rootfs_path: str
    kernel_path: str
    boot_args: str
    arch: str
    backend: str
    size_bytes: int
    cached: bool
    warnings: list[str]


class RemovedEntry(TypedDict):
    name: str
    path: str
    size_bytes: int


class ImageRmPayload(TypedDict):
    removed: list[RemovedEntry]
    freed_bytes: int


def _fail(
    command_name: str,
    message: str,
    *,
    json_output: bool,
    code: str = "runtime_error",
    recovery: str | None = None,
    exit_code: int = 1,
) -> int:
    """Report a failure in JSON or Rich form and return the exit code."""
    if json_output:
        return emit_error(command_name, code, message, recovery=recovery, exit_code=exit_code)
    render_error(message, hint=recovery)
    return exit_code


def _entry_kind(path: Path, root: Path) -> str:
    """The row kind an entry directory would get, without walking it."""
    if path.parent == root / _MACOS_DIR:
        return "macos"
    if path.parent != root:
        return "custom"
    if _IMAGE_DIR_NAME_RE.match(path.name):
        return "image"
    if _KERNEL_DIR_NAME_RE.match(path.name):
        return "kernel"
    return "other"


def _brief_error(exc: BaseException, limit: int = 160) -> str:
    """One short line of an exception, for user-facing messages.

    Full tracebacks, multi-line output, and long URLs belong in logs, not
    in error envelopes.
    """
    text = str(exc).strip()
    text = text.splitlines()[0] if text else exc.__class__.__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _classify(path: Path, size_bytes: int | None = None) -> ImageRow:
    """Build an :class:`ImageRow` for one child directory of the image dir."""
    name = path.name
    size = size_bytes if size_bytes is not None else _total_size(path)
    created = _created_iso(path)
    image_match = _IMAGE_DIR_NAME_RE.match(name)
    if image_match:
        return ImageRow(
            name=name,
            kind="image",
            preset=image_match["preset"],
            version=image_match["version"],
            arch=image_match["arch"],
            vmm=image_match["vmm"],
            os=image_match["os"] or "ubuntu",
            current=image_match["version"] == __version__,
            created=created,
            size_bytes=size,
            path=str(path),
        )
    kernel_match = _KERNEL_DIR_NAME_RE.match(name)
    if kernel_match:
        return ImageRow(
            name=name,
            kind="kernel",
            preset=None,
            version=kernel_match["version"],
            arch=kernel_match["arch"],
            vmm=None,
            os=None,
            current=kernel_match["version"] == __version__,
            created=created,
            size_bytes=size,
            path=str(path),
        )
    return ImageRow(
        name=name,
        kind="other",
        preset=None,
        version=None,
        arch=None,
        vmm=None,
        os=None,
        current=None,
        created=created,
        size_bytes=size,
        path=str(path),
    )


def _custom_row(fingerprint_dir: Path, size_bytes: int | None = None) -> ImageRow:
    """Build an :class:`ImageRow` for one ``custom/<name>/<fingerprint>`` dir."""
    arch: str | None = None
    metadata = fingerprint_dir / "metadata.json"
    if metadata.is_file():
        try:
            parsed = json.loads(metadata.read_text())
            if isinstance(parsed, dict) and isinstance(parsed.get("arch"), str):
                arch = parsed["arch"]
        except (OSError, ValueError):
            arch = None
    return ImageRow(
        name=f"{_CUSTOM_DIR}/{fingerprint_dir.parent.name}",
        kind="custom",
        preset=None,
        version=fingerprint_dir.name[:12],
        arch=arch,
        vmm=None,
        os=None,
        current=None,
        created=_created_iso(fingerprint_dir),
        size_bytes=size_bytes if size_bytes is not None else _total_size(fingerprint_dir),
        path=str(fingerprint_dir),
    )


def _macos_row(path: Path, size_bytes: int | None = None) -> ImageRow:
    """Build one row for a local-only macOS base image."""
    version: str | None = None
    current: bool | None = None
    manifest = path / _MACOS_MANIFEST
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                raw_version = payload.get("guest_version")
                version = raw_version if isinstance(raw_version, str) else None
                from smolvm.host.lume import LUME_VERSION

                current = payload.get("driver_version") == LUME_VERSION
        except (OSError, ValueError):
            pass
    return ImageRow(
        name=f"{_MACOS_DIR}/{path.name}",
        kind="macos",
        preset=None,
        version=version,
        arch="arm64",
        vmm="vz",
        os="macos",
        current=current,
        created=_created_iso(path),
        size_bytes=size_bytes if size_bytes is not None else _total_size(path),
        path=str(path),
    )


def _macos_rows(macos_root: Path) -> list[ImageRow]:
    if not macos_root.is_dir():
        return []
    return [
        _macos_row(child)
        for child in sorted(macos_root.iterdir())
        if child.is_dir() and not child.name.startswith(".")
    ]


def _custom_rows(custom_root: Path) -> list[ImageRow]:
    """One row per built custom image (``custom/<name>/<fingerprint>``)."""
    rows: list[ImageRow] = []
    for name_dir in sorted(custom_root.iterdir()):
        if not name_dir.is_dir() or name_dir.name.startswith("."):
            continue
        for fingerprint_dir in sorted(name_dir.iterdir()):
            if not fingerprint_dir.is_dir() or fingerprint_dir.name.startswith("."):
                continue
            rows.append(_custom_row(fingerprint_dir))
    return rows


def _cached_rows(root: Path) -> list[ImageRow]:
    """Classify every cache entry under *root* (empty when it's missing).

    Top-level dot-directories (e.g. an interrupted load's ``.partial``
    staging dir) are shown as "other" rows so their disk usage is never
    invisible to the user.
    """
    if not root.is_dir():
        return []
    rows: list[ImageRow] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == _CUSTOM_DIR:
            rows.extend(_custom_rows(child))
        elif child.name == _MACOS_DIR:
            rows.extend(_macos_rows(child))
        else:
            rows.append(_classify(child))
    return rows


def _normalize_rm_name(name: str, root: Path) -> str:
    """Normalize what the user typed to a cache-entry name.

    Accepts trailing path separators (shell tab completion) and the
    absolute path that ``image list`` prints, as long as it points inside
    the image directory.
    """
    requested = name.rstrip("/\\")
    candidate = Path(requested)
    if candidate.is_absolute():
        for base in (root, root.resolve()):
            try:
                return candidate.relative_to(base).as_posix()
            except ValueError:
                continue
    return requested


def _resolve_entries(requested: str, root: Path) -> list[Path]:
    """Match a validated entry name to cache directories.

    Exact names (including the ``custom/...`` namespace) win; a custom
    fingerprint may be shortened to a unique prefix (the 12 characters
    ``image list`` shows are enough); otherwise the name is treated as a
    preset and every cached variant matches.

    Raises ``ValueError`` when a fingerprint prefix matches more than one
    build — callers surface it as an invalid-input error.
    """
    if "/" in requested:
        segments = requested.split("/")
        # A symlinked intermediate component could redirect the whole
        # operation at a different entry; refuse rather than follow.
        for depth in range(1, len(segments)):
            if (root / "/".join(segments[:depth])).is_symlink():
                return []
        exact = root / requested
        if exact.is_dir() or exact.is_symlink():
            return [exact]
        if len(segments) == 3:
            # Docker-style short ids: a unique fingerprint prefix works.
            name_dir = root / segments[0] / segments[1]
            if name_dir.is_dir():
                prefix_matches = [
                    child
                    for child in sorted(name_dir.iterdir())
                    if child.is_dir()
                    and not child.name.startswith(".")
                    and child.name.startswith(segments[2])
                ]
                if len(prefix_matches) > 1:
                    options = ", ".join(
                        f"{segments[0]}/{segments[1]}/{child.name}" for child in prefix_matches
                    )
                    raise ValueError(
                        f"'{requested}' matches more than one build. Pick one of: {options}."
                    )
                return prefix_matches
        return []
    exact = root / requested
    if exact.is_dir() or exact.is_symlink():
        return [exact]
    macos_exact = root / _MACOS_DIR / requested
    if macos_exact.is_dir() or macos_exact.is_symlink():
        return [macos_exact]
    if not root.is_dir():
        return []
    canonical = _canonical_preset(requested)
    matches: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        match = _IMAGE_DIR_NAME_RE.match(child.name)
        if match and match["preset"] == canonical:
            matches.append(child)
    return matches


def _expand_custom_entries(matches: list[Path], root: Path) -> list[Path]:
    """Expand a ``custom/<name>`` match into its fingerprint directories."""
    expanded: list[Path] = []
    for match in matches:
        if match.parent == root / _CUSTOM_DIR:
            expanded.extend(
                child
                for child in sorted(match.iterdir())
                if child.is_dir() and not child.name.startswith(".")
            )
        else:
            expanded.append(match)
    return expanded


def _download_progress() -> Progress:
    """The download progress renderer shared with the sandbox boot flow."""
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
        console=console_stdout(),
        transient=True,
    )


def _pull_retry_command(
    preset: str,
    arch: str | None,
    vmm: str | None,
    os_name: str | None,
    image_dir: str | None,
) -> str:
    """The user's pull invocation, reconstructed for recovery messages."""
    parts = ["smolvm", "image", "pull", preset]
    if arch:
        parts += ["--arch", arch]
    if vmm:
        parts += ["--vmm", vmm]
    if os_name:
        parts += ["--os", os_name]
    if image_dir:
        parts += ["--image-dir", shlex.quote(image_dir)]
    return " ".join(parts)


def _build_retry_command(
    tag: str,
    context_path: str,
    dockerfile: str | None,
    size_mb: int,
    build_args: tuple[str, ...],
    arch: str | None,
    backend: str,
    init: str,
    image_dir: str | None,
) -> str:
    """The user's build invocation, reconstructed for recovery messages."""
    parts = ["smolvm", "image", "build", "-t", tag]
    if dockerfile:
        parts += ["-f", shlex.quote(dockerfile)]
    if size_mb != 512:
        parts += ["--size-mb", str(size_mb)]
    for item in build_args:
        parts += ["--build-arg", shlex.quote(item)]
    if arch:
        parts += ["--arch", arch]
    if backend != "auto":
        parts += ["--backend", backend]
    if init != "/init":
        parts += ["--init", shlex.quote(init)]
    if image_dir:
        parts += ["--image-dir", shlex.quote(image_dir)]
    parts.append(shlex.quote(context_path))
    return " ".join(parts)


def _pull_all_retry_command(
    arch: str | None,
    vmm: str | None,
    os_name: str | None,
    image_dir: str | None,
) -> str:
    """The user's ``pull --all`` invocation, reconstructed for recovery."""
    parts = ["smolvm", "image", "pull", "--all"]
    if arch:
        parts += ["--arch", arch]
    if vmm:
        parts += ["--vmm", vmm]
    if os_name:
        parts += ["--os", os_name]
    if image_dir:
        parts += ["--image-dir", shlex.quote(image_dir)]
    return " ".join(parts)


def _non_default_dir_warnings(root: Path) -> list[str]:
    """Warn when downloads land somewhere sandbox starts will not look."""
    default_root = resolve_image_dir(None)
    if root == default_root:
        return []
    return [
        f"Sandboxes look for images in '{default_root}'. Set "
        f"SMOLVM_IMAGE_DIR={shlex.quote(str(root))} so they use this download."
    ]


def _execute_pull(
    *,
    preset: str,
    arch: str,
    vmm: str,
    os_name: str,
    root: Path,
    progress: Progress | None,
    label_prefix: str = "",
) -> ImagePullPayload:
    """Download one published image, reporting progress; raises on failure.

    The (preset, arch, vmm, os) tuple must already be validated against the
    manifest by the caller.
    """
    from smolvm.images.published import Arch, Os, Preset, Vmm, cache_name, ensure_published_image

    name = cache_name(
        cast("Preset", preset), cast("Arch", arch), cast("Vmm", vmm), os=cast("Os", os_name)
    )
    target_dir = root / name

    # "Already cached" must mean the pull was a no-op. Downloads are
    # observable through on_download, but rootfs decompression is not, so
    # also watch the cache directory itself: any file (re)created in it —
    # by download or decompression — bumps its mtime.
    pre_mtime = target_dir.stat().st_mtime_ns if target_dir.is_dir() else None
    downloaded = False
    download_tasks: dict[str, Any] = {}

    def on_download(label: str, chunk: int, total: int | None) -> None:
        nonlocal downloaded
        downloaded = True
        if progress is None:
            return
        key = f"{label_prefix}{label}"
        if key not in download_tasks:
            download_tasks[key] = progress.add_task(f"Downloading {key}", total=total)
        progress.update(download_tasks[key], advance=chunk)

    local = ensure_published_image(
        cast("Preset", preset),
        cast("Arch", arch),
        cast("Vmm", vmm),
        cast("Os", os_name),
        cache_dir=root,
        on_download=on_download,
    )

    post_mtime = target_dir.stat().st_mtime_ns if target_dir.is_dir() else None
    already_cached = not downloaded and pre_mtime is not None and post_mtime == pre_mtime

    return ImagePullPayload(
        preset=preset,
        arch=arch,
        vmm=vmm,
        os=os_name,
        version=__version__,
        name=name,
        kernel_path=str(local.kernel_path),
        rootfs_path=str(local.rootfs_path),
        size_bytes=_total_size(target_dir),
        already_cached=already_cached,
        warnings=[],
    )


def run_image_pull(
    *,
    preset: str,
    arch: str | None = None,
    vmm: str | None = None,
    os_name: str | None = None,
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.pull",
) -> int:
    """Execute ``smolvm image pull``."""
    from smolvm.cli.main import _host_arch_for_published, _vmm_for_host
    from smolvm.exceptions import ImageError
    from smolvm.images.published import MANIFEST, is_preset_published

    canonical = _canonical_preset(preset)
    retry_command = _pull_retry_command(preset, arch, vmm, os_name, image_dir)

    try:
        resolved_arch = arch or _host_arch_for_published()
        resolved_vmm = vmm or _vmm_for_host()
    except RuntimeError:
        return _fail(
            command_name,
            "SmolVM doesn't publish prebuilt images for this machine. Pass the "
            f"platform explicitly, e.g. 'smolvm image pull {preset} --arch amd64 "
            "--vmm firecracker'.",
            json_output=json_output,
            code="invalid_input",
            exit_code=2,
        )
    resolved_os = os_name or "ubuntu"

    if not is_preset_published(canonical, resolved_arch, resolved_vmm, resolved_os):  # type: ignore[arg-type]
        known_presets = sorted({key[0] for key in MANIFEST})
        if canonical not in known_presets:
            return _fail(
                command_name,
                f"'{preset}' is not a published image. Choose one of: {', '.join(known_presets)}.",
                json_output=json_output,
                code="invalid_input",
                exit_code=2,
            )
        return _fail(
            command_name,
            f"No published image for '{preset}' on "
            f"{resolved_arch}/{resolved_vmm} with os '{resolved_os}'. Run "
            f"'smolvm image pull {preset}' to use the defaults for this machine.",
            json_output=json_output,
            code="invalid_input",
            exit_code=2,
        )

    root = resolve_image_dir(image_dir)
    progress_cm: AbstractContextManager[Progress | None] = (
        nullcontext(None) if json_output else _download_progress()
    )
    try:
        with progress_cm as progress:
            if progress is not None:
                # Visible immediately: SHA verification of cached files and
                # rootfs decompression run without download callbacks and
                # can take a while on multi-GB images.
                progress.add_task("Preparing image...", total=None)
            payload = _execute_pull(
                preset=canonical,
                arch=resolved_arch,
                vmm=resolved_vmm,
                os_name=resolved_os,
                root=root,
                progress=progress,
            )
    except ImageError as exc:
        return _fail(
            command_name,
            f"Could not download the image: {_brief_error(exc)}",
            json_output=json_output,
            recovery=f"Check your network connection and retry '{retry_command}'.",
        )
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not save the image: {_brief_error(exc)}",
            json_output=json_output,
            recovery=f"Free up disk space or fix permissions on '{root}', "
            f"then retry '{retry_command}'.",
        )
    except Exception as exc:
        return _fail(
            command_name,
            f"Could not get the image: {_brief_error(exc)}",
            json_output=json_output,
            recovery=f"Retry '{retry_command}'.",
        )

    payload["warnings"] = _non_default_dir_warnings(root)
    if json_output:
        return emit_success(command_name, payload)

    console = console_stdout()
    size = _format_bytes(payload["size_bytes"])
    if payload["already_cached"]:
        console.print(f"Already cached: [bold]{payload['name']}[/bold] ({size})")
    else:
        console.print(f"Pulled [bold]{payload['name']}[/bold] ({size})")
    console.print(f"  kernel: {payload['kernel_path']}")
    console.print(f"  rootfs: {payload['rootfs_path']}")
    for warning in payload["warnings"]:
        console.print(f"[yellow]{warning}[/yellow]")
    return 0


def run_image_pull_all(
    *,
    arch: str | None = None,
    vmm: str | None = None,
    os_name: str | None = None,
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.pull",
) -> int:
    """Execute ``smolvm image pull --all``."""
    from smolvm.cli.main import _host_arch_for_published, _vmm_for_host
    from smolvm.images.published import Arch, Vmm, published_targets

    retry_command = _pull_all_retry_command(arch, vmm, os_name, image_dir)

    try:
        resolved_arch = arch or _host_arch_for_published()
        resolved_vmm = vmm or _vmm_for_host()
    except RuntimeError:
        return _fail(
            command_name,
            "SmolVM doesn't publish prebuilt images for this machine. Pass the "
            "platform explicitly, e.g. 'smolvm image pull --all --arch amd64 "
            "--vmm firecracker'.",
            json_output=json_output,
            code="invalid_input",
            exit_code=2,
        )

    targets = published_targets(cast("Arch", resolved_arch), cast("Vmm", resolved_vmm))
    if os_name:
        targets = [(p, o) for (p, o) in targets if o == os_name]
    if not targets:
        no_filter_command = _pull_all_retry_command(arch, vmm, None, image_dir)
        return _fail(
            command_name,
            "No published images match this machine and the options you passed.",
            json_output=json_output,
            code="invalid_input",
            recovery=f"Run '{no_filter_command}' to download every available image.",
            exit_code=2,
        )

    root = resolve_image_dir(image_dir)
    pulled: list[ImagePullPayload] = []
    failed: list[PullFailure] = []
    progress_cm: AbstractContextManager[Progress | None] = (
        nullcontext(None) if json_output else _download_progress()
    )
    with progress_cm as progress:
        overall = (
            progress.add_task("Preparing images...", total=None) if progress is not None else None
        )
        for target_preset, target_os in targets:
            if progress is not None and overall is not None:
                progress.update(overall, description=f"Preparing {target_preset} ({target_os})...")
            try:
                pulled.append(
                    _execute_pull(
                        preset=target_preset,
                        arch=resolved_arch,
                        vmm=resolved_vmm,
                        os_name=target_os,
                        root=root,
                        progress=progress,
                        label_prefix=f"{target_preset}/{target_os} ",
                    )
                )
            except Exception as exc:
                failed.append(
                    PullFailure(preset=target_preset, os=target_os, error=_brief_error(exc))
                )

    payload = ImagePullAllPayload(
        arch=resolved_arch,
        vmm=resolved_vmm,
        pulled=pulled,
        failed=failed,
        warnings=_non_default_dir_warnings(root),
    )

    failure_recovery = (
        f"Check your network connection and disk space, then retry '{retry_command}'."
    )
    if json_output:
        if failed:
            return emit_error(
                command_name,
                "runtime_error",
                f"Could not download {len(failed)} of {len(targets)} images.",
                recovery=failure_recovery,
                details=payload,
                exit_code=1,
            )
        return emit_success(command_name, payload)

    console = console_stdout()
    for item in pulled:
        state = "Already cached" if item["already_cached"] else "Pulled"
        console.print(f"{state}: [bold]{item['name']}[/bold] ({_format_bytes(item['size_bytes'])})")
    for failure in failed:
        console.print(
            f"[red]Failed: {failure['preset']} ({failure['os']}) — {failure['error']}[/red]"
        )
    for warning in payload["warnings"]:
        console.print(f"[yellow]{warning}[/yellow]")
    if failed:
        render_error(
            f"Could not download {len(failed)} of {len(targets)} images.",
            hint=failure_recovery,
        )
        return 1
    return 0


def run_image_list(
    *,
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.list",
) -> int:
    """Execute ``smolvm image list``."""
    root = resolve_image_dir(image_dir)
    try:
        rows = _cached_rows(root)
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not read the image folder '{root}': {_brief_error(exc)}",
            json_output=json_output,
            recovery="Fix the folder's permissions and run 'smolvm image list' again.",
        )
    payload = ImageListPayload(
        image_dir=str(root),
        images=rows,
        total_size_bytes=sum(row["size_bytes"] for row in rows),
    )
    if json_output:
        return emit_success(command_name, payload)

    if not rows:
        render_empty(
            "Images",
            f"No cached images in {root}. Run 'smolvm image pull codex' to download one.",
        )
        return 0

    from rich.table import Table

    table = Table(title="Cached Images")
    # Fold long names instead of truncating: the full name is the handle
    # `smolvm image rm <name>` needs.
    table.add_column("Name", overflow="fold")
    table.add_column("Kind")
    table.add_column("Platform")
    table.add_column("Version")
    table.add_column("Created")
    table.add_column("Size", justify="right")
    for row in rows:
        platform_parts = [part for part in (row["arch"], row["vmm"], row["os"]) if part]
        version = row["version"] or "-"
        if row["current"] is False:
            version = f"[yellow]{version} (stale)[/yellow]"
        table.add_row(
            row["name"],
            row["kind"],
            "/".join(platform_parts) or "-",
            version,
            _relative_time(row["created"]),
            _format_bytes(row["size_bytes"]),
        )
    console = console_stdout()
    console.print(table)
    console.print(
        f"Total: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}, "
        f"{_format_bytes(payload['total_size_bytes'])} in {root}"
    )
    return 0


def _inspect_entry(path: Path, root: Path) -> ImageInspectEntry:
    """Build the detailed inspect record for one cache directory."""
    files: list[ImageFileEntry] = []
    try:
        for f in sorted(path.rglob("*")):
            try:
                if not f.is_file():
                    continue
                st = f.stat()
                files.append(
                    ImageFileEntry(
                        name=f.relative_to(path).as_posix(),
                        size_bytes=st.st_size,
                        size_on_disk_bytes=_size_on_disk(st),
                    )
                )
            except OSError:
                continue
    except OSError:
        pass

    # The files walk already collected on-disk sizes — reuse them for the
    # row instead of walking the tree a second time.
    size_on_disk = sum(f["size_on_disk_bytes"] for f in files)
    entry_kind = _entry_kind(path, root)
    if entry_kind == "custom":
        row = _custom_row(path, size_bytes=size_on_disk)
    elif entry_kind == "macos":
        row = _macos_row(path, size_bytes=size_on_disk)
    else:
        row = _classify(path, size_bytes=size_on_disk)

    sidecar_path = path / "rootfs.ext4.from-sha256"
    sidecar: str | None = None
    if sidecar_path.is_file():
        try:
            sidecar = sidecar_path.read_text().strip()
        except OSError:
            sidecar = None

    manifest_info: ImageManifestInfo | None = None
    if row["kind"] == "image" and row["current"]:
        from smolvm.exceptions import ImageError
        from smolvm.images.published import IMAGES_RELEASE_TAG, lookup

        try:
            entry = lookup(row["preset"], row["arch"], row["vmm"], row["os"])  # type: ignore[arg-type]
        except ImageError:
            entry = None
        if entry is not None:
            manifest_info = ImageManifestInfo(
                kernel_url=entry.kernel_url,
                kernel_sha256=entry.kernel_sha256,
                rootfs_url=entry.rootfs_url,
                rootfs_sha256=entry.rootfs_sha256,
                images_release_tag=IMAGES_RELEASE_TAG,
            )

    inspected = ImageInspectEntry(
        **row,
        files=files,
        rootfs_sidecar=sidecar,
        manifest=manifest_info,
    )
    if row["kind"] == "macos":
        try:
            from smolvm.macos.images import MacOSImageManifest

            macos_manifest = MacOSImageManifest.model_validate_json(
                (path / _MACOS_MANIFEST).read_text(encoding="utf-8")
            )
            inspected["macos"] = MacOSImageInfo(
                guest_version=macos_manifest.guest_version,
                source_ipsw=macos_manifest.source_ipsw,
                logical_size_bytes=macos_manifest.disk_size_bytes,
                allocated_size_bytes=macos_manifest.allocated_size_bytes,
                driver_version=macos_manifest.driver_version,
            )
        except (OSError, ValueError):
            pass
    return inspected


def run_image_inspect(
    *,
    name: str,
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.inspect",
) -> int:
    """Execute ``smolvm image inspect``."""
    root = resolve_image_dir(image_dir)
    requested = _normalize_rm_name(name, root)

    if not _valid_entry_name(requested):
        return _fail(
            command_name,
            f"'{name}' is not a cached image name.",
            json_output=json_output,
            code="invalid_input",
            recovery="Run 'smolvm image list' to see cached images.",
            exit_code=2,
        )

    try:
        matches = _expand_custom_entries(_resolve_entries(requested, root), root)
    except ValueError as exc:
        return _fail(
            command_name,
            str(exc),
            json_output=json_output,
            code="invalid_input",
            recovery="Retry with one of the full names above.",
            exit_code=2,
        )
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not read the image folder '{root}': {_brief_error(exc)}",
            json_output=json_output,
            recovery="Fix the folder's permissions, then retry "
            f"'smolvm image inspect {requested}'.",
        )

    if not matches:
        return _fail(
            command_name,
            f"No cached image named '{requested}'.",
            json_output=json_output,
            recovery="Run 'smolvm image list' to see cached images.",
        )

    entries = [_inspect_entry(path, root) for path in matches]
    if json_output:
        # Docker-style: inspect always returns an array.
        return emit_success(command_name, entries)

    from rich.table import Table

    console = console_stdout()
    for entry in entries:
        table = Table(title=entry["name"], show_header=False)
        table.add_column("Field")
        table.add_column("Value", overflow="fold")
        table.add_row("Kind", entry["kind"])
        if entry["preset"]:
            table.add_row("Preset", entry["preset"])
        platform_parts = [p for p in (entry["arch"], entry["vmm"], entry["os"]) if p]
        table.add_row("Platform", "/".join(platform_parts) or "-")
        version = entry["version"] or "-"
        if entry["current"] is False:
            version = f"[yellow]{version} (stale)[/yellow]"
        table.add_row("Version", version)
        created = entry["created"]
        table.add_row("Created", f"{_relative_time(created)} ({created})" if created else "-")
        table.add_row("Size", _format_bytes(entry["size_bytes"]))
        table.add_row("Path", entry["path"])
        macos = entry.get("macos")
        if macos is not None:
            table.add_row("Source IPSW", macos["source_ipsw"])
            table.add_row("Logical size", _format_bytes(macos["logical_size_bytes"]))
            table.add_row("Allocated size", _format_bytes(macos["allocated_size_bytes"]))
            table.add_row("Runtime version", macos["driver_version"])
        manifest = entry["manifest"]
        if manifest is not None:
            table.add_row("Release tag", manifest["images_release_tag"])
            table.add_row("Kernel URL", manifest["kernel_url"])
            table.add_row("Kernel SHA-256", manifest["kernel_sha256"])
            table.add_row("Rootfs URL", manifest["rootfs_url"])
            table.add_row("Rootfs SHA-256", manifest["rootfs_sha256"])
        if entry["rootfs_sidecar"]:
            table.add_row("Rootfs sidecar", entry["rootfs_sidecar"])
        if entry["files"]:
            file_lines = "\n".join(
                f"{f['name']}  {_format_bytes(f['size_bytes'])}"
                f" ({_format_bytes(f['size_on_disk_bytes'])} on disk)"
                for f in entry["files"]
            )
            table.add_row("Files", file_lines)
        console.print(table)
    return 0


def run_image_rm(
    *,
    name: str,
    image_dir: str | None = None,
    dry_run: bool = False,
    json_output: bool = False,
    command_name: str = "image.rm",
) -> int:
    """Execute ``smolvm image rm``."""
    root = resolve_image_dir(image_dir)
    requested = _normalize_rm_name(name, root)

    if not _valid_entry_name(requested):
        return _fail(
            command_name,
            f"'{name}' is not a cached image name.",
            json_output=json_output,
            code="invalid_input",
            recovery="Run 'smolvm image list' to see cached images.",
            exit_code=2,
        )

    permission_recovery = f"Fix the folder's permissions, then retry 'smolvm image rm {requested}'."
    try:
        matches = _resolve_entries(requested, root)
    except ValueError as exc:
        return _fail(
            command_name,
            str(exc),
            json_output=json_output,
            code="invalid_input",
            recovery="Retry with one of the full names above.",
            exit_code=2,
        )
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not read the image folder '{root}': {_brief_error(exc)}",
            json_output=json_output,
            recovery=permission_recovery,
        )

    if not matches:
        return _fail(
            command_name,
            f"No cached image named '{requested}'.",
            json_output=json_output,
            recovery="Run 'smolvm image list' to see cached images.",
        )

    entries: list[RemovedEntry] = []
    for target in matches:
        display = target.relative_to(root).as_posix()
        if target.is_symlink():
            # A link is removed with unlink() — what it points at is never
            # touched — so removing it frees no space.
            entries.append(RemovedEntry(name=display, path=str(target), size_bytes=0))
            continue
        # Names and intermediate components are validated above, so a real
        # directory escaping its parent should be impossible; keep a hard
        # stop in front of the recursive delete anyway: the target must
        # resolve to a direct child of its own (resolved) parent inside the
        # image directory.
        resolved_parent = target.parent.resolve()
        resolved_root = root.resolve()
        inside_root = resolved_parent == resolved_root or resolved_root in resolved_parent.parents
        if target.resolve().parent != resolved_parent or not inside_root:
            return _fail(
                command_name,
                f"'{display}' could not be removed safely.",
                json_output=json_output,
                recovery=f"Delete it yourself if needed: '{target}'.",
            )
        entries.append(RemovedEntry(name=display, path=str(target), size_bytes=_total_size(target)))

    freed = sum(entry["size_bytes"] for entry in entries)

    if dry_run:
        if json_output:
            return emit_success(
                command_name,
                {"dry_run": True, "would_remove": entries, "would_free_bytes": freed},
            )
        console = console_stdout()
        console.print(
            f"[bold]Would remove {len(entries)} image(s) ({_format_bytes(freed)}):[/bold]"
        )
        for entry in entries:
            console.print(f"  {entry['name']}  ({_format_bytes(entry['size_bytes'])})")
        return 0

    for target, entry in zip(matches, entries, strict=True):
        try:
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target)
        except OSError as exc:
            return _fail(
                command_name,
                f"Could not remove '{entry['name']}': {exc}",
                json_output=json_output,
                recovery=permission_recovery,
            )

    payload = ImageRmPayload(removed=entries, freed_bytes=freed)
    if json_output:
        return emit_success(command_name, payload)
    console = console_stdout()
    console.print(f"Removed {len(entries)} image(s), freed {_format_bytes(freed)}.")
    return 0


def _build_macos_image_with_progress(
    manager: Any,
    *,
    name: str,
    ipsw: str | Path,
) -> Any:
    """Render one continuous bar across download, install, and desktop setup."""
    from smolvm.macos.models import MacOSInstallProgress

    download_started = False
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console_stdout(),
    ) as progress:
        task = progress.add_task("Starting macOS image preparation…", total=100)

        def update(event: MacOSInstallProgress) -> None:
            nonlocal download_started
            percent = event.percent or 0
            if event.phase == "download":
                download_started = True
                completed = percent * 0.35
                description = f"Downloading macOS… {percent}%"
            elif event.phase == "install":
                base = 35 if download_started else 0
                span = 50 if download_started else 85
                completed = base + percent * span / 100
                description = f"Installing macOS… {percent}%"
            elif event.phase == "setup":
                completed = 90
                description = "Setting up the macOS desktop…"
            else:
                completed = 100
                description = "macOS image ready"
            progress.update(task, completed=completed, description=description)

        result = manager.build(name=name, ipsw=ipsw, on_progress=update)
        progress.update(task, completed=100, description="macOS image ready")
        return result


def run_macos_image_build(
    *,
    tag: str,
    ipsw: str,
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.build",
) -> int:
    """Build one local-only macOS base image through the pinned VZ driver."""
    if not _SAFE_SEGMENT_RE.fullmatch(tag) or tag.startswith("."):
        return _fail(
            command_name,
            f"'{tag}' is not a valid image name.",
            json_output=json_output,
            code="invalid_input",
            recovery="Use letters, numbers, dots, underscores, or hyphens for -t/--tag.",
            exit_code=2,
        )
    ipsw_value: str | Path = ipsw
    if ipsw != "latest":
        ipsw_path = Path(ipsw).expanduser().resolve()
        if not ipsw_path.is_file():
            return _fail(
                command_name,
                f"macOS restore image was not found: '{ipsw_path}'.",
                json_output=json_output,
                code="invalid_input",
                recovery=f"Restore the file, or run 'smolvm image build --os macos "
                f"--ipsw latest -t {tag}'.",
                exit_code=2,
            )
        ipsw_value = ipsw_path

    from smolvm.macos.images import MacOSImageManager
    from smolvm.runtime.backends import BACKEND_VZ, ensure_backend_available

    try:
        ensure_backend_available(BACKEND_VZ)
    except Exception as exc:
        return _fail(
            command_name,
            str(exc),
            json_output=json_output,
            recovery="Run 'smolvm setup --macos', then 'smolvm doctor --backend vz'.",
        )

    root = resolve_image_dir(image_dir) / "macos"
    try:
        manager = MacOSImageManager(image_dir=root)
        if not json_output:
            reuse = ipsw == "latest" and manager.find_cached_latest_ipsw() is not None
            preparation = (
                "Reusing the downloaded Apple restore file."
                if reuse
                else "Preparing macOS from Apple. This can take 20–40 minutes the first time."
            )
            console_stdout().print(
                f"{preparation} Build logs are stored in '{root / f'{tag}.build.log'}'."
            )
        manifest = (
            manager.build(name=tag, ipsw=ipsw_value)
            if json_output
            else _build_macos_image_with_progress(
                manager,
                name=tag,
                ipsw=ipsw_value,
            )
        )
        payload = {
            "name": manifest.name,
            "os": "macos",
            "guest_version": manifest.guest_version,
            "source_ipsw": manifest.source_ipsw,
            "driver": manifest.driver,
            "driver_version": manifest.driver_version,
            "disk_size_bytes": manifest.disk_size_bytes,
            "allocated_size_bytes": manifest.allocated_size_bytes,
            "path": str(manager.bundle_path(tag)),
            "local_only": True,
        }
        if json_output:
            return emit_success(command_name, payload)
        console_stdout().print(
            f"macOS image '{tag}' is ready. Create a desktop with "
            f"'smolvm sandbox create --os macos --name test-mac'."
        )
        return 0
    except Exception as exc:
        return _fail(
            command_name,
            str(exc),
            json_output=json_output,
            recovery=(
                "Run 'smolvm image build --os macos "
                f"--ipsw {shlex.quote(ipsw)} -t {shlex.quote(tag)}' again."
            ),
        )


def run_image_build(
    *,
    tag: str,
    context_path: str,
    dockerfile: str | None = None,
    size_mb: int = 512,
    build_args: tuple[str, ...] = (),
    arch: str | None = None,
    backend: str = "auto",
    init: str = "/init",
    image_dir: str | None = None,
    json_output: bool = False,
    command_name: str = "image.build",
) -> int:
    """Execute ``smolvm image build``."""
    from smolvm.exceptions import ImageError
    from smolvm.images.boot import DirectKernelBoot
    from smolvm.images.builder import DockerContextValue, DockerRootfsBuilder

    retry_command = _build_retry_command(
        tag, context_path, dockerfile, size_mb, build_args, arch, backend, init, image_dir
    )

    if not _SAFE_SEGMENT_RE.match(tag) or tag.startswith("."):
        # The builder's own charset is slightly looser: ".." would escape
        # the custom/ namespace, and dot-prefixed names would be hidden by
        # 'image list'.
        return _fail(
            command_name,
            f"'{tag}' can't be used as an image name. Use letters, numbers, "
            "dots, dashes, and underscores.",
            json_output=json_output,
            code="invalid_input",
            exit_code=2,
        )

    context_dir = Path(context_path)
    if not context_dir.is_dir():
        return _fail(
            command_name,
            f"'{context_path}' is not a folder.",
            json_output=json_output,
            code="invalid_input",
            recovery="Pass the folder with the files your Dockerfile copies "
            f"(use '.' for the current one), e.g. 'smolvm image build -t {tag} .'.",
            exit_code=2,
        )

    dockerfile_path = Path(dockerfile) if dockerfile else context_dir / "Dockerfile"
    try:
        dockerfile_text = dockerfile_path.read_text()
    except OSError:
        return _fail(
            command_name,
            f"No Dockerfile at '{dockerfile_path}'.",
            json_output=json_output,
            code="invalid_input",
            recovery="Create it, or point at one with the -f option.",
            exit_code=2,
        )

    parsed_args: dict[str, str] = {}
    for item in build_args:
        key, sep, value = item.partition("=")
        if not sep or not key:
            return _fail(
                command_name,
                f"'{item}' is not a valid build argument.",
                json_output=json_output,
                code="invalid_input",
                recovery="Use KEY=VALUE, e.g. --build-arg VERSION=1.2.",
                exit_code=2,
            )
        parsed_args[key] = value

    # Everything in the context folder is sent to the build. Files named
    # "Dockerfile" (any depth) are reserved by the builder and skipped.
    context: dict[str, DockerContextValue] = {}
    warnings: list[str] = []
    try:
        resolved_dockerfile = dockerfile_path.resolve()
        for f in sorted(context_dir.rglob("*")):
            if f.is_symlink() or not f.is_file():
                continue
            if f.resolve() == resolved_dockerfile:
                continue
            if f.name.lower() == "dockerfile":
                skipped_path = f.relative_to(context_dir).as_posix()
                warnings.append(
                    f"Skipped '{skipped_path}': files named Dockerfile can't be "
                    "part of the build context. Rename it to include it."
                )
                continue
            context[f.relative_to(context_dir).as_posix()] = f
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not read the folder '{context_path}': {_brief_error(exc)}",
            json_output=json_output,
            recovery=f"Fix the folder's permissions, then retry '{retry_command}'.",
        )

    try:
        builder = DockerRootfsBuilder(
            name=tag,
            dockerfile=dockerfile_text,
            context=context,
            rootfs_size_mb=size_mb,
            cache_dir=resolve_image_dir(image_dir),
            build_args=parsed_args,
            ssh_capable=False,
        )
    except ValueError as exc:
        return _fail(
            command_name,
            _brief_error(exc),
            json_output=json_output,
            code="invalid_input",
            recovery=f"Fix the value above, then retry '{retry_command}'.",
            exit_code=2,
        )

    # Clock-free cache detection: a build was reused iff its rootfs existed
    # before the call and was not rewritten (wall-clock comparisons break
    # under VM clock drift and coarse filesystem timestamps).
    existing_builds: dict[str, int] = {}
    tag_dir = builder.cache_dir / _CUSTOM_DIR / tag
    if tag_dir.is_dir():
        for child in tag_dir.iterdir():
            try:
                if child.is_dir():
                    existing_builds[child.name] = (child / "rootfs.ext4").stat().st_mtime_ns
            except OSError:
                continue

    try:
        boot_image = builder.build_boot_image(
            backend=backend,
            arch=arch or "host",
            boot=DirectKernelBoot(init=init),
        )
    except (ImageError, ValueError) as exc:
        return _fail(
            command_name,
            _brief_error(exc, limit=400),
            json_output=json_output,
            code="invalid_input" if isinstance(exc, ValueError) else "runtime_error",
            recovery=f"Fix the problem above, then retry '{retry_command}'.",
            exit_code=2 if isinstance(exc, ValueError) else 1,
        )
    except OSError as exc:
        return _fail(
            command_name,
            f"Could not write the image: {_brief_error(exc)}",
            json_output=json_output,
            recovery=f"Free up disk space, then retry '{retry_command}'.",
        )

    fingerprint_dir = boot_image.rootfs_path.parent
    try:
        cached = (
            existing_builds.get(fingerprint_dir.name) == boot_image.rootfs_path.stat().st_mtime_ns
        )
    except OSError:
        cached = False

    payload = ImageBuildPayload(
        name=tag,
        fingerprint=fingerprint_dir.name,
        rootfs_path=str(boot_image.rootfs_path),
        kernel_path=str(boot_image.kernel_path),
        boot_args=boot_image.render_boot_args(),
        arch=str(boot_image.arch or arch or "host"),
        backend=str(boot_image.backend or backend),
        size_bytes=_total_size(fingerprint_dir),
        cached=cached,
        warnings=warnings,
    )
    if json_output:
        return emit_success(command_name, payload)

    from rich.panel import Panel

    console = console_stdout()
    for warning in warnings:
        console.print(f"[yellow]{warning}[/yellow]")
    verb = "Reusing cached" if cached else "Built"
    console.print(
        Panel.fit(
            f"{verb} custom image [bold]{_CUSTOM_DIR}/{tag}[/bold] "
            f"({_format_bytes(payload['size_bytes'])})\n"
            f"  rootfs: {payload['rootfs_path']}\n"
            f"  kernel: {payload['kernel_path']}\n\n"
            "Boot it from Python with:\n"
            "  [bold]from smolvm import BootImage, DirectKernelBoot, SmolVM\n"
            f"  image = BootImage(name={tag!r},\n"
            f"                    rootfs_path={payload['rootfs_path']!r},\n"
            f"                    rootfs_format='raw-ext4',\n"
            f"                    kernel_path={payload['kernel_path']!r},\n"
            f"                    boot=DirectKernelBoot(init={init!r}))\n"
            "  with SmolVM.from_image(image) as vm:\n"
            "      vm.start()[/bold]\n\n"
            f"Manage it with 'smolvm image list' and 'smolvm image rm {_CUSTOM_DIR}/{tag}'.",
            title="Custom image ready",
            border_style="green",
        )
    )
    return 0
