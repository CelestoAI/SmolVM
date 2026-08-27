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

"""Portable regression tests for the packaged Linux setup scripts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INSTALL_SCRIPT = _REPO_ROOT / "scripts" / "internal" / "install-firecracker.sh"
_SYSTEM_SETUP_SCRIPT = _REPO_ROOT / "scripts" / "system-setup.sh"
_ONE_LINE_INSTALLER = _REPO_ROOT / "scripts" / "install.sh"
_RUNTIME_CONFIG_SCRIPT = _REPO_ROOT / "scripts" / "internal" / "configure-runtime-sudoers.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _fake_download_tools(tmp_path: Path) -> Path:
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_executable(
        tools / "uname",
        '#!/bin/bash\n[[ "$1" == "-m" ]] && echo x86_64 || /usr/bin/uname "$@"\n',
    )
    _write_executable(
        tools / "wget",
        """#!/bin/bash
set -eu
while [[ $# -gt 0 ]]; do
    if [[ "$1" == "-O" ]]; then
        : > "$2"
        exit 0
    fi
    shift
done
exit 1
""",
    )
    _write_executable(
        tools / "tar",
        """#!/bin/bash
set -eu
if [[ "${FAIL_FAKE_TAR:-}" == "1" ]]; then
    exit 2
fi
destination=""
while [[ $# -gt 0 ]]; do
    if [[ "$1" == "-C" ]]; then
        destination="$2"
        shift 2
        continue
    fi
    shift
done
release="$destination/release-v1.14.1-x86_64"
mkdir -p "$release"
cat > "$release/firecracker-v1.14.1-x86_64" <<'EOF'
#!/bin/sh
echo 'Firecracker v1.14.1'
EOF
chmod 755 "$release/firecracker-v1.14.1-x86_64"
""",
    )
    return tools


def _run_installer(
    destination: Path,
    tools: Path,
    *,
    fail_tar: bool = False,
    runtime_user: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{tools}{os.pathsep}{env['PATH']}"
    if fail_tar:
        env["FAIL_FAKE_TAR"] = "1"
    args = [
        "bash",
        str(_INSTALL_SCRIPT),
        "--skip-deps",
        "--firecracker-dir",
        str(destination),
    ]
    if runtime_user is not None:
        args.extend(["--runtime-user", runtime_user])
    return subprocess.run(
        args,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_installer_supports_custom_directory_with_spaces(tmp_path: Path) -> None:
    tools = _fake_download_tools(tmp_path)
    destination = tmp_path / "custom runtime" / "bin"

    result = _run_installer(destination, tools)

    assert result.returncode == 0, result.stderr or result.stdout
    binary = destination / "firecracker"
    assert binary.is_file()
    assert os.access(binary, os.X_OK)
    version_output = subprocess.check_output([binary], text=True)
    assert "Firecracker v1.14.1" in version_output


def test_explicit_directory_does_not_require_runtime_user_home(tmp_path: Path) -> None:
    if subprocess.run(["id", "nobody"], check=False, capture_output=True).returncode != 0:
        pytest.skip("test requires the standard nobody account")

    tools = _fake_download_tools(tmp_path)
    _write_executable(tools / "getent", "#!/bin/sh\nexit 1\n")
    destination = tmp_path / "explicit" / "bin"

    result = _run_installer(destination, tools, runtime_user="nobody")

    assert result.returncode == 0, result.stderr or result.stdout
    assert (destination / "firecracker").is_file()


def test_failed_reinstall_preserves_existing_binary(tmp_path: Path) -> None:
    tools = _fake_download_tools(tmp_path)
    destination = tmp_path / "bin"
    destination.mkdir()
    binary = destination / "firecracker"
    binary.write_text("#!/bin/sh\necho existing\n")
    binary.chmod(0o755)

    result = _run_installer(destination, tools, fail_tar=True)

    assert result.returncode != 0
    assert binary.read_text() == "#!/bin/sh\necho existing\n"


@pytest.mark.parametrize(
    "script",
    [_INSTALL_SCRIPT, _SYSTEM_SETUP_SCRIPT, _ONE_LINE_INSTALLER, _RUNTIME_CONFIG_SCRIPT],
)
def test_changed_setup_scripts_have_valid_bash_syntax(script: Path) -> None:
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_firecracker_installer_has_no_system_destination_or_jailer_state() -> None:
    text = _INSTALL_SCRIPT.read_text() + _SYSTEM_SETUP_SCRIPT.read_text()

    assert "/usr/local/bin/firecracker" not in text
    assert "/usr/local/bin/jailer" not in text
    assert "/srv/jailer" not in text
    assert "groupadd -g 2000 firecracker" not in text


def test_system_setup_handles_fedora_without_assuming_apt() -> None:
    text = _SYSTEM_SETUP_SCRIPT.read_text()

    assert "/run/ostree-booted" in text
    assert "rpm-ostree install" in text
    assert "dnf install" in text
    assert "Host dependencies already installed" in text


def test_one_line_installer_keeps_custom_directory_for_doctor() -> None:
    text = _ONE_LINE_INSTALLER.read_text()

    assert "FIRECRACKER_DIR_ARG" in text
    assert 'export SMOLVM_FIRECRACKER_DIR="${FIRECRACKER_DIR_ARG}"' in text
    assert 'smolvm setup --skip-deps "${SETUP_ARGS[@]}"' in text


def test_runtime_sudo_policy_excludes_user_writable_programs() -> None:
    text = _RUNTIME_CONFIG_SCRIPT.read_text()

    assert 'LOOPFS_HELPER_DIR="/var/lib/smolvm/libexec"' in text
    assert '[[ -L "${LOOPFS_HELPER_DST}"' in text
    assert "SMOLVM_VM_CMDS" not in text
    assert "FIRECRACKER_BIN" not in text
    assert "kill -9" not in text
    assert "SMOLVM_FIRECRACKER_DIR" not in text


def test_setup_recovery_shell_quotes_custom_firecracker_directory() -> None:
    text = _SYSTEM_SETUP_SCRIPT.read_text()

    assert "printf -v firecracker_dir_arg '%q'" in text
