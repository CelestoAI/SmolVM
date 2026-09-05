# Copyright 2026 Celesto AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smolvm.exceptions import ImageError
from smolvm.macos.images import MacOSImageManager
from smolvm.macos.models import LumeVMDetails


def _details(name: str = "macos-latest") -> LumeVMDetails:
    return LumeVMDetails.model_validate(
        {
            "name": name,
            "os": "macOS 26",
            "cpuCount": 4,
            "memorySize": 8 * 1024**3,
            "diskSize": {"allocated": 20 * 1024**3, "total": 80 * 1024**3},
            "display": "1440x900",
            "status": "stopped",
            "provisioningOperation": None,
            "vncUrl": None,
            "ipAddress": None,
            "sshAvailable": True,
            "locationName": "test",
            "sharedDirectories": [],
            "networkMode": "nat",
            "downloadProgress": None,
        }
    )


def test_build_and_resolve_local_macos_image(tmp_path: Path) -> None:
    driver = MagicMock()
    driver.inspect.return_value = _details()
    driver.version.return_value = "0.4.0"
    image_dir = tmp_path / "images" / "macos"
    driver.install_base_image.side_effect = lambda *args, **kwargs: (
        image_dir / "macos-latest"
    ).mkdir(parents=True)
    manager = MacOSImageManager(image_dir=image_dir, driver=driver)
    manager._check_storage = MagicMock()  # type: ignore[method-assign]

    manifest = manager.build(name="macos-latest", ipsw="latest")
    loaded = manager.get("macos-latest")
    machine = manager.machine_config("mac-test", data_dir=tmp_path / "data")

    assert loaded == manifest
    assert manifest.guest_version == "macOS 26"
    assert manifest.allocated_size_bytes == 20 * 1024**3
    assert machine.base_image == "macos-latest"
    assert machine.bundle_path == tmp_path / "data" / "macos-vms" / "mac-test"
    driver.install_base_image.assert_called_once()


def test_latest_build_reuses_completed_lume_download(tmp_path: Path) -> None:
    image_dir = tmp_path / "images" / "macos"
    lume_temp = tmp_path / "latest.ipsw"
    with zipfile.ZipFile(lume_temp, "w") as archive:
        archive.writestr("BuildManifest.plist", "test")
    driver = MagicMock()
    driver.inspect.return_value = _details()
    driver.version.return_value = "0.4.0"
    used_ipsw: list[Path] = []

    def install(request, **kwargs):  # type: ignore[no-untyped-def]
        used_ipsw.append(Path(request.ipsw))
        (image_dir / "macos-latest").mkdir(parents=True)

    driver.install_base_image.side_effect = install
    manager = MacOSImageManager(image_dir=image_dir, driver=driver)
    manager._check_storage = MagicMock()  # type: ignore[method-assign]

    with (
        patch("smolvm.macos.images.tempfile.gettempdir", return_value=str(tmp_path)),
        patch("smolvm.macos.images._MINIMUM_IPSW_BYTES", 1),
    ):
        manifest = manager.build(ipsw="latest")

    assert used_ipsw == [image_dir / ".downloads" / "latest.ipsw"]
    assert manifest.source_ipsw == "latest"
    assert not lume_temp.exists()
    assert not manager.cached_latest_ipsw_path.exists()


def test_failed_install_keeps_completed_download_for_retry(tmp_path: Path) -> None:
    image_dir = tmp_path / "images" / "macos"
    lume_temp = tmp_path / "latest.ipsw"
    with zipfile.ZipFile(lume_temp, "w") as archive:
        archive.writestr("BuildManifest.plist", "test")
    driver = MagicMock()
    driver.install_base_image.side_effect = RuntimeError("install failed")
    manager = MacOSImageManager(image_dir=image_dir, driver=driver)
    manager._check_storage = MagicMock()  # type: ignore[method-assign]

    with (
        patch("smolvm.macos.images.tempfile.gettempdir", return_value=str(tmp_path)),
        patch("smolvm.macos.images._MINIMUM_IPSW_BYTES", 1),
        pytest.raises(RuntimeError, match="install failed"),
    ):
        manager.build(ipsw="latest")

    assert not lume_temp.exists()
    assert manager.cached_latest_ipsw_path.is_file()


def test_completed_bundle_survives_metadata_error_and_recovers(tmp_path: Path) -> None:
    image_dir = tmp_path / "images" / "macos"
    driver = MagicMock()
    driver.version.return_value = "0.4.0"
    driver.inspect.side_effect = [ValueError("temporary schema mismatch"), _details()]
    driver.install_base_image.side_effect = lambda *args, **kwargs: (
        image_dir / "macos-latest"
    ).mkdir(parents=True)
    manager = MacOSImageManager(image_dir=image_dir, driver=driver)
    manager._check_storage = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="schema mismatch"):
        manager.build(ipsw="latest")

    assert (image_dir / "macos-latest").is_dir()
    manifest = manager.build(ipsw="latest")

    assert manifest.guest_version == "macOS 26"
    assert driver.install_base_image.call_count == 1


def test_image_name_cannot_escape_managed_folder(tmp_path: Path) -> None:
    manager = MacOSImageManager(image_dir=tmp_path / "images", driver=MagicMock())

    with pytest.raises(ImageError, match="not a valid macOS image name"):
        manager.get("../outside")


def test_build_rejects_unsafe_name_before_locking(tmp_path: Path) -> None:
    image_dir = tmp_path / "images"
    manager = MacOSImageManager(image_dir=image_dir, driver=MagicMock())

    with pytest.raises(ImageError, match="not a valid macOS image name"):
        manager.build(name="../outside")

    assert not image_dir.exists()


def test_local_ipsw_must_be_an_existing_apple_restore_file(tmp_path: Path) -> None:
    manager = MacOSImageManager(image_dir=tmp_path / "images", driver=MagicMock())
    manager._check_storage = MagicMock()  # type: ignore[method-assign]

    with pytest.raises(ImageError, match="--ipsw latest"):
        manager.build(ipsw=tmp_path / "missing.ipsw")


def test_missing_image_error_names_build_command(tmp_path: Path) -> None:
    manager = MacOSImageManager(image_dir=tmp_path / "images", driver=MagicMock())

    with pytest.raises(ImageError, match="smolvm image build --os macos"):
        manager.get("missing")
