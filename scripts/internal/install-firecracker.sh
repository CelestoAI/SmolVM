#!/bin/bash

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

# install-firecracker.sh - Internal helper to install Firecracker.
# Intended to be called by scripts/system-setup.sh.
set -euo pipefail

WITH_IMAGES=false
SKIP_DEPS=false
REQUIRE_KVM=false
FC_VERSION_OVERRIDE=""
FIRECRACKER_DIR_OVERRIDE=""
RUNTIME_USER_OVERRIDE=""

usage() {
    cat <<EOF_USAGE
Usage: $(basename "$0") [options]

Downloads and installs Firecracker without changing operating-system packages
when --skip-deps is used.

Options:
  --with-images               Download kernel/rootfs images after install
  --skip-deps                 Do not install wget or tar
  --require-kvm               Fail if /dev/kvm is missing
  --firecracker-version <ver> Pin Firecracker release tag (default: built-in or
                              \$SMOLVM_FIRECRACKER_VERSION / \$FC_VERSION env)
  --firecracker-dir <dir>     Folder for Firecracker (default:
                              \$SMOLVM_FIRECRACKER_DIR or ~/.smolvm/bin)
  --runtime-user <user>       User who will run Firecracker (internal setup option)
  -h, --help                  Show this help
EOF_USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-images)
            WITH_IMAGES=true
            ;;
        --skip-deps)
            SKIP_DEPS=true
            ;;
        --require-kvm)
            REQUIRE_KVM=true
            ;;
        --firecracker-version)
            if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
                echo "❌ --firecracker-version needs a release tag such as 'v1.14.1'."
                exit 1
            fi
            if [[ ! "$2" =~ ^[A-Za-z0-9._-]+$ ]]; then
                echo "❌ Firecracker version '$2' contains unsupported characters; use a tag such as 'v1.14.1'."
                exit 1
            fi
            FC_VERSION_OVERRIDE="$2"
            shift
            ;;
        --firecracker-dir)
            if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
                echo "❌ --firecracker-dir needs a folder; for example, run 'smolvm setup --firecracker-dir \"$HOME/.local/bin\"'."
                exit 1
            fi
            FIRECRACKER_DIR_OVERRIDE="$2"
            shift
            ;;
        --runtime-user)
            if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
                echo "❌ --runtime-user needs the account that will run SmolVM."
                exit 1
            fi
            RUNTIME_USER_OVERRIDE="$2"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "❌ Unknown option '$1'; run '$(basename "$0") --help' to see supported options."
            exit 1
            ;;
    esac
    shift
done

run_root() {
    if [[ ${EUID} -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        echo "❌ Administrator permission is needed; install sudo or run this command as root."
        return 1
    fi
}

resolve_runtime_user() {
    if [[ -n "${RUNTIME_USER_OVERRIDE}" ]]; then
        printf '%s\n' "${RUNTIME_USER_OVERRIDE}"
    elif [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        printf '%s\n' "${SUDO_USER}"
    elif [[ ${EUID} -ne 0 ]]; then
        id -un
    else
        printf '%s\n' "root"
    fi
}

resolve_user_home() {
    local user="$1"
    local home=""
    if command -v getent >/dev/null 2>&1; then
        home="$(getent passwd "${user}" 2>/dev/null | cut -d: -f6 || true)"
    fi
    if [[ -z "${home}" && "${user}" == "$(id -un)" ]]; then
        home="${HOME:-}"
    fi
    if [[ -z "${home}" ]]; then
        echo "❌ Home folder for '${user}' was not found; rerun with '--firecracker-dir /absolute/path'." >&2
        return 1
    fi
    printf '%s\n' "${home}"
}

runtime_user="$(resolve_runtime_user)"
if ! id "${runtime_user}" >/dev/null 2>&1; then
    echo "❌ User '${runtime_user}' does not exist; rerun with '--runtime-user $(id -un)'."
    exit 1
fi
runtime_group="$(id -gn "${runtime_user}")"
runtime_home="$(resolve_user_home "${runtime_user}")"

if [[ -n "${FIRECRACKER_DIR_OVERRIDE}" ]]; then
    FIRECRACKER_DIR="${FIRECRACKER_DIR_OVERRIDE}"
elif [[ -n "${SMOLVM_FIRECRACKER_DIR+x}" ]]; then
    if [[ -z "${SMOLVM_FIRECRACKER_DIR//[[:space:]]/}" ]]; then
        echo "❌ SMOLVM_FIRECRACKER_DIR is empty; unset it or set it to an absolute folder."
        exit 1
    fi
    FIRECRACKER_DIR="${SMOLVM_FIRECRACKER_DIR}"
else
    FIRECRACKER_DIR="${runtime_home}/.smolvm/bin"
fi

if [[ "${FIRECRACKER_DIR}" != /* ]]; then
    echo "❌ Firecracker folder must be absolute: '${FIRECRACKER_DIR}'; rerun with '--firecracker-dir /absolute/path'."
    exit 1
fi
if [[ -e "${FIRECRACKER_DIR}" && ! -d "${FIRECRACKER_DIR}" ]]; then
    echo "❌ Firecracker folder is a file: '${FIRECRACKER_DIR}'; choose another folder with '--firecracker-dir'."
    exit 1
fi

user_scoped_dir=false
if [[ "${FIRECRACKER_DIR}" == "${runtime_home}" || "${FIRECRACKER_DIR}" == "${runtime_home}/"* ]]; then
    user_scoped_dir=true
fi

if [[ -n "${FC_VERSION_OVERRIDE}" ]]; then
    FC_VERSION="${FC_VERSION_OVERRIDE}"
elif [[ -n "${SMOLVM_FIRECRACKER_VERSION:-}" ]]; then
    FC_VERSION="${SMOLVM_FIRECRACKER_VERSION}"
else
    FC_VERSION="${FC_VERSION:-v1.14.1}"
fi
if [[ ! "${FC_VERSION}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "❌ Firecracker version '${FC_VERSION}' is invalid; use a tag such as 'v1.14.1'."
    exit 1
fi

ARCH="$(uname -m)"
if [[ "${ARCH}" != "x86_64" && "${ARCH}" != "aarch64" ]]; then
    echo "❌ Firecracker does not support this machine architecture: '${ARCH}'."
    exit 1
fi

if [[ "${REQUIRE_KVM}" == "true" && ! -e /dev/kvm ]]; then
    echo "❌ Hardware virtualization is missing; enable KVM, then rerun this command."
    exit 1
fi

missing_tools=()
for cmd in wget tar; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
        missing_tools+=("${cmd}")
    fi
done

if [[ ${#missing_tools[@]} -ne 0 ]]; then
    if [[ "${SKIP_DEPS}" == "true" ]]; then
        echo "❌ Required commands are missing: ${missing_tools[*]}; install them, then rerun this command."
        exit 1
    fi
    if [[ -e /run/ostree-booted ]] && command -v rpm-ostree >/dev/null 2>&1; then
        echo "❌ Required commands are missing: ${missing_tools[*]}; run 'sudo rpm-ostree install ${missing_tools[*]}', reboot, then rerun this command."
        exit 1
    elif command -v apt-get >/dev/null 2>&1; then
        run_root apt-get update -qq
        run_root apt-get install -y -qq "${missing_tools[@]}"
    elif command -v dnf >/dev/null 2>&1; then
        run_root dnf install -y "${missing_tools[@]}"
    else
        echo "❌ Required commands are missing: ${missing_tools[*]}; install them with your operating system, then rerun this command."
        exit 1
    fi
fi

work_dir="$(mktemp -d)"
staged_path=""
cleanup() {
    rm -rf "${work_dir}"
    if [[ -n "${staged_path}" && -e "${staged_path}" ]]; then
        rm -f "${staged_path}" 2>/dev/null || run_root rm -f "${staged_path}" || true
    fi
}
trap cleanup EXIT

tarball_path="${work_dir}/firecracker.tgz"
release_dir="${work_dir}/release-${FC_VERSION}-${ARCH}"
source_binary="${release_dir}/firecracker-${FC_VERSION}-${ARCH}"
url="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz"

echo "Downloading Firecracker ${FC_VERSION} for ${ARCH}..."
wget -q -O "${tarball_path}" "${url}"
tar -xzf "${tarball_path}" -C "${work_dir}"
if [[ ! -f "${source_binary}" ]]; then
    echo "❌ The downloaded Firecracker archive does not contain '${source_binary##*/}'; retry the setup."
    exit 1
fi

if [[ ${EUID} -eq 0 && "${runtime_user}" != "root" && "${user_scoped_dir}" == "true" ]]; then
    if [[ "${FIRECRACKER_DIR}" == "${runtime_home}/.smolvm/bin" ]]; then
        install -d -o "${runtime_user}" -g "${runtime_group}" -m 0755 \
            "${runtime_home}/.smolvm" "${FIRECRACKER_DIR}"
    else
        install -d -o "${runtime_user}" -g "${runtime_group}" -m 0755 "${FIRECRACKER_DIR}"
    fi
elif ! mkdir -p "${FIRECRACKER_DIR}" 2>/dev/null; then
    run_root install -d -m 0755 "${FIRECRACKER_DIR}"
fi

staged_path="${FIRECRACKER_DIR}/.firecracker.$$.tmp"
if ! install -m 0755 "${source_binary}" "${staged_path}" 2>/dev/null; then
    run_root install -m 0755 "${source_binary}" "${staged_path}"
fi
if ! mv -f "${staged_path}" "${FIRECRACKER_DIR}/firecracker" 2>/dev/null; then
    run_root mv -f "${staged_path}" "${FIRECRACKER_DIR}/firecracker"
fi
staged_path=""
if [[ ${EUID} -eq 0 && "${runtime_user}" != "root" && "${user_scoped_dir}" == "true" ]]; then
    chown "${runtime_user}:${runtime_group}" "${FIRECRACKER_DIR}/firecracker"
fi

if [[ ! -x "${FIRECRACKER_DIR}/firecracker" ]]; then
    echo "❌ Firecracker was not installed at '${FIRECRACKER_DIR}/firecracker'; restore write access to that folder and retry."
    exit 1
fi

echo "✅ Firecracker ${FC_VERSION} installed at '${FIRECRACKER_DIR}/firecracker'."
"${FIRECRACKER_DIR}/firecracker" --version 2>&1 | head -1

if [[ "${WITH_IMAGES}" == "true" ]]; then
    echo ""
    bash "$(cd "$(dirname "$0")/.." && pwd)/download-images.sh"
fi
