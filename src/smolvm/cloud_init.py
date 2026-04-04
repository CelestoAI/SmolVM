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

"""Cloud-init helpers for prebuilt SmolVM guest images."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from uuid import uuid4

from pycdlib import PyCdlib


def default_user_data(ssh_public_key: str) -> str:
    """Return cloud-init user-data for a root SSH login."""
    return f"""#cloud-config
disable_root: false
ssh_pwauth: false
users:
  - default
  - name: root
    lock_passwd: true
    shell: /bin/bash
    ssh_authorized_keys:
      - {ssh_public_key}
write_files:
  - path: /etc/ssh/sshd_config.d/10-smolvm-root.conf
    permissions: "0644"
    content: |
      PermitRootLogin yes
      PasswordAuthentication no
runcmd:
  - >
    systemctl restart ssh || systemctl restart sshd ||
    service ssh restart || service sshd restart || true
"""


def default_meta_data(*, instance_id: str = "smolvm-default", hostname: str = "smolvm") -> str:
    """Return cloud-init meta-data for a default SmolVM guest."""
    return f"""instance-id: {instance_id}
local-hostname: {hostname}
"""


def seed_cache_key(*, ssh_public_key: str, instance_id: str, hostname: str) -> str:
    """Return a stable cache key for a cloud-init seed payload."""
    digest = hashlib.sha256(
        f"{ssh_public_key}\n{instance_id}\n{hostname}\n".encode()
    ).hexdigest()
    return digest[:16]


def build_seed_iso(
    output_path: Path,
    *,
    user_data: str,
    meta_data: str,
    vendor_data: str | None = None,
) -> Path:
    """Create a NoCloud ISO image at *output_path*."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output_path = output_path.with_name(
        f".{output_path.name}.{uuid4().hex}.tmp"
    )

    iso = PyCdlib()
    try:
        try:
            iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="CIDATA")

            _add_text_file(iso, "/USERDATA.;1", "user-data", user_data)
            _add_text_file(iso, "/METADATA.;1", "meta-data", meta_data)
            if vendor_data is not None:
                _add_text_file(iso, "/VENDORDA.;1", "vendor-data", vendor_data)

            iso.write(str(temp_output_path))
        finally:
            iso.close()

        temp_output_path.replace(output_path)
    except Exception:
        temp_output_path.unlink(missing_ok=True)
        raise
    return output_path


def _add_text_file(iso: PyCdlib, iso_path: str, rr_name: str, content: str) -> None:
    data = content.encode("utf-8")
    iso.add_fp(
        io.BytesIO(data),
        len(data),
        iso_path=iso_path,
        joliet_path=f"/{rr_name}",
        rr_name=rr_name,
    )
