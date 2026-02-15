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

# configure-runtime-sudoers.sh - Configure scoped NOPASSWD sudo for SmolVM runtime.
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_USER="${SUDO_USER:-}"
CHECK_ONLY=false
REMOVE=false
LOOPFS_HELPER_SRC="${SCRIPT_DIR}/internal/image-build-loopfs.sh"
LOOPFS_HELPER_DST="/usr/local/libexec/smolvm-loopfs-helper"

usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [--runtime-user <user>] [--check-only] [--remove]

Configures scoped sudoers rules so SmolVM runtime commands can run without
interactive password prompts.

Options:
  --runtime-user <user>   Target non-root user (default: \$SUDO_USER).
  --check-only            Validate existing runtime sudoers and command access.
  --remove                Remove generated runtime sudoers file.
  -h, --help              Show this help.
EOF
}

if [[ ${EUID} -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo -E "$0" "$@"
    fi
    echo "❌ This script must run as root (sudo not found)."
    exit 1
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runtime-user)
            if [[ $# -lt 2 ]]; then
                echo "❌ --runtime-user requires a value"
                usage
                exit 1
            fi
            RUNTIME_USER="$2"
            shift
            ;;
        --check-only)
            CHECK_ONLY=true
            ;;
        --remove)
            REMOVE=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "❌ Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
    shift
done

if [[ -z "${RUNTIME_USER}" ]]; then
    echo "❌ Runtime user is required. Pass --runtime-user <user> or run via sudo as that user."
    exit 1
fi

if ! id "${RUNTIME_USER}" >/dev/null 2>&1; then
    echo "❌ User does not exist: ${RUNTIME_USER}"
    exit 1
fi

IP_BIN="$(command -v ip || true)"
IPTABLES_BIN="$(command -v iptables || true)"
SYSCTL_BIN="$(command -v sysctl || true)"
VISUDO_BIN="$(command -v visudo || true)"
FIRECRACKER_BIN="$(command -v firecracker || true)"
INSTALL_BIN="$(command -v install || true)"

if [[ -z "${FIRECRACKER_BIN}" && -x "/usr/local/bin/firecracker" ]]; then
    FIRECRACKER_BIN="/usr/local/bin/firecracker"
fi

for item in \
    "ip:${IP_BIN}" \
    "iptables:${IPTABLES_BIN}" \
    "sysctl:${SYSCTL_BIN}" \
    "visudo:${VISUDO_BIN}" \
    "firecracker:${FIRECRACKER_BIN}" \
    "install:${INSTALL_BIN}"
do
    name="${item%%:*}"
    path="${item#*:}"
    if [[ -z "${path}" ]]; then
        echo "❌ Required command not found: ${name}"
        exit 1
    fi
done

SUDOERS_FILE="/etc/sudoers.d/smolvm-runtime-${RUNTIME_USER}"

install_loopfs_helper() {
    if [[ ! -f "${LOOPFS_HELPER_SRC}" ]]; then
        echo "❌ Required helper script not found: ${LOOPFS_HELPER_SRC}"
        exit 1
    fi
    mkdir -p "$(dirname "${LOOPFS_HELPER_DST}")"
    "${INSTALL_BIN}" -o root -g root -m 0755 "${LOOPFS_HELPER_SRC}" "${LOOPFS_HELPER_DST}"
}

render_sudoers() {
    local target_file="$1"
    cat > "${target_file}" <<EOF
# Managed by SmolVM (${SCRIPT_NAME}) for user ${RUNTIME_USER}
# Allows only commands needed by SmolVM runtime networking, Firecracker, and image mount helper.
Defaults:${RUNTIME_USER} !requiretty
Cmnd_Alias SMOLVM_NET_CMDS = ${IP_BIN} *, ${IPTABLES_BIN} *, ${SYSCTL_BIN} net.ipv4.ip_forward, ${SYSCTL_BIN} -w net.ipv4.ip_forward=1
Cmnd_Alias SMOLVM_VM_CMDS = ${FIRECRACKER_BIN} *, /bin/kill -9 *, /usr/bin/kill -9 *
Cmnd_Alias SMOLVM_IMG_CMDS = ${LOOPFS_HELPER_DST} *
${RUNTIME_USER} ALL=(root) NOPASSWD: SMOLVM_NET_CMDS, SMOLVM_VM_CMDS, SMOLVM_IMG_CMDS
EOF
}

validate_sudoers_file() {
    local file_path="$1"
    "${VISUDO_BIN}" -cf "${file_path}" >/dev/null
}

check_runtime_access() {
    local failures=()
    local runner=(sudo -n -u "${RUNTIME_USER}" sudo -n)

    if ! "${runner[@]}" "${IP_BIN}" link show >/dev/null 2>&1; then
        failures+=("ip")
    fi
    if ! "${runner[@]}" "${IPTABLES_BIN}" -L >/dev/null 2>&1; then
        failures+=("iptables")
    fi
    if ! "${runner[@]}" "${SYSCTL_BIN}" net.ipv4.ip_forward >/dev/null 2>&1; then
        failures+=("sysctl")
    fi
    if ! "${runner[@]}" "${FIRECRACKER_BIN}" --version >/dev/null 2>&1; then
        failures+=("firecracker")
    fi
    if ! "${runner[@]}" "${LOOPFS_HELPER_DST}" --help >/dev/null 2>&1; then
        failures+=("image-build-loopfs-helper")
    fi

    if [[ ${#failures[@]} -ne 0 ]]; then
        echo "❌ Runtime sudo access check failed for: ${failures[*]}"
        return 1
    fi

    echo "✅ Runtime sudo access check passed for user '${RUNTIME_USER}'"
    return 0
}

if [[ "${REMOVE}" == "true" ]]; then
    if [[ -f "${SUDOERS_FILE}" ]]; then
        rm -f "${SUDOERS_FILE}"
        echo "✅ Removed runtime sudoers: ${SUDOERS_FILE}"
    else
        echo "ℹ️ Runtime sudoers not present: ${SUDOERS_FILE}"
    fi
    exit 0
fi

if [[ "${CHECK_ONLY}" == "true" ]]; then
    if [[ ! -f "${SUDOERS_FILE}" ]]; then
        echo "❌ Runtime sudoers file missing: ${SUDOERS_FILE}"
        exit 1
    fi
    validate_sudoers_file "${SUDOERS_FILE}"
    if [[ ! -x "${LOOPFS_HELPER_DST}" ]]; then
        echo "❌ Runtime helper missing or not executable: ${LOOPFS_HELPER_DST}"
        exit 1
    fi
    check_runtime_access
    exit 0
fi

tmp_file="$(mktemp)"
trap 'rm -f "${tmp_file}"' EXIT
install_loopfs_helper
render_sudoers "${tmp_file}"
validate_sudoers_file "${tmp_file}"
"${INSTALL_BIN}" -m 0440 "${tmp_file}" "${SUDOERS_FILE}"

echo "✅ Installed runtime sudoers: ${SUDOERS_FILE}"
echo "✅ Installed runtime helper: ${LOOPFS_HELPER_DST}"
check_runtime_access
