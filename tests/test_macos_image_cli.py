# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

import json
import shlex
from pathlib import Path
from unittest.mock import patch

from smolvm.cli.image import (
    run_image_inspect,
    run_image_list,
    run_image_rm,
    run_macos_image_build,
)
from smolvm.cli.image_transfer import run_image_save


def _image(root: Path) -> Path:
    image = root / "macos" / "macos-latest"
    image.mkdir(parents=True)
    (image / "disk.img").write_bytes(b"disk")
    (image / "smolvm-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "macos-latest",
                "guest_version": "macOS 26",
                "guest_build": None,
                "source_ipsw": "latest",
                "cpu_count": 4,
                "memory_mib": 8192,
                "disk_size_bytes": 80 * 1024**3,
                "allocated_size_bytes": 20 * 1024**3,
                "driver": "lume",
                "driver_version": "0.4.0",
                "host_arch": "arm64",
                "created_at": "2026-07-24T00:00:00Z",
            }
        )
    )
    return image


def test_macos_image_build_recovery_quotes_ipsw_path(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    ipsw_path = tmp_path / "Apple Restore.ipsw"
    ipsw_path.touch()
    ipsw = str(ipsw_path)
    with (
        patch("smolvm.runtime.backends.ensure_backend_available"),
        patch(
            "smolvm.macos.images.MacOSImageManager",
            side_effect=RuntimeError("build failed"),
        ),
    ):
        result = run_macos_image_build(
            tag="macos-latest",
            ipsw=ipsw,
            image_dir=str(tmp_path),
            json_output=True,
        )

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    resolved_ipsw = str(ipsw_path.resolve())
    assert f"--ipsw {shlex.quote(resolved_ipsw)} -t macos-latest" in payload["error"]["recovery"]


def test_image_list_and_inspect_classify_local_macos_image(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _image(tmp_path)

    assert run_image_list(image_dir=str(tmp_path), json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    row = listed["data"]["images"][0]
    assert row["name"] == "macos/macos-latest"
    assert row["kind"] == "macos"
    assert row["vmm"] == "vz"

    assert run_image_inspect(name="macos-latest", image_dir=str(tmp_path), json_output=True) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["data"][0]["kind"] == "macos"
    assert inspected["data"][0]["macos"] == {
        "guest_version": "macOS 26",
        "source_ipsw": "latest",
        "logical_size_bytes": 80 * 1024**3,
        "allocated_size_bytes": 20 * 1024**3,
        "driver_version": "0.4.0",
    }


def test_image_rm_accepts_macos_shorthand(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    image = _image(tmp_path)

    assert run_image_rm(name="macos-latest", image_dir=str(tmp_path), json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["removed"][0]["name"] == "macos/macos-latest"
    assert not image.exists()


def test_image_save_refuses_local_macos_image(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    _image(tmp_path)

    result = run_image_save(
        name="macos-latest",
        output=str(tmp_path / "image.tar"),
        image_dir=str(tmp_path),
        json_output=True,
    )

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "unsupported_operation"
    assert not (tmp_path / "image.tar").exists()
