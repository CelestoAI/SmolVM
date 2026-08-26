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

# system-setup.sh - System-level setup for SmolVM (no Python/venv).
# Installs Firecracker and host dependencies. Docker is optional.
# Can optionally configure command-scoped NOPASSWD sudo for runtime operations.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SCRIPT="${SCRIPT_DIR}/internal/install-firecracker.sh"
RUNTIME_CONFIG_SCRIPT="${SCRIPT_DIR}/internal/configure-runtime-sudoers.sh"

ORIGINAL_ARGS=("$@")

if [[ ${EUID} -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo -E "$0" "${ORIGINAL_ARGS[@]}"
    fi
    echo "❌ This script must be run as root (sudo not found)."
    exit 1
fi

CHECK_ONLY=false
WITH_DOCKER=false
SKIP_DEPS=false
CONFIGURE_RUNTIME=false
REMOVE_RUNTIME_CONFIG=false
RUNTIME_USER=""
SKIP_KVM_CHECK=false
SKIP_RUNTIME_CHECK=false
FIRECRACKER_VERSION=""
FIRECRACKER_DIR=""
FIRECRACKER_DIR_CONFIGURED=false

usage() {
    cat <<EOF_USAGE
Usage: $(basename "$0") [options]

Installs host dependencies and Firecracker (no Python/venv involvement).

Options:
  --check-only                   Only validate system prerequisites; do not install.
  --with-docker                  Install Docker (required for SSH image demo).
  --skip-deps                    Do not install missing operating-system packages.
  --configure-runtime            Configure scoped NOPASSWD sudoers for SmolVM runtime.
  --remove-runtime-config        Remove generated runtime sudoers config.
  --runtime-user <user>          Target user for runtime sudoers/docker group (default: invoking user).
  --for-bake                     Bake-friendly install: implies --skip-kvm-check and
                                 --skip-runtime-check. Use during AMI builds, then run
                                 'smolvm doctor' on the runtime host to verify.
  --skip-kvm-check               Do not require /dev/kvm at install time.
  --skip-runtime-check           Skip the post-install sudoers self-test on the live host.
  --firecracker-version <ver>    Pin Firecracker release tag (e.g. v1.14.1). Falls back
                                 to \$SMOLVM_FIRECRACKER_VERSION or the built-in default.
  --firecracker-dir <dir>        Folder for Firecracker (default:
                                 \$SMOLVM_FIRECRACKER_DIR or ~/.smolvm/bin).
  -h, --help                     Show this help.
EOF_USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check-only)
            CHECK_ONLY=true
            ;;
        --with-docker)
            WITH_DOCKER=true
            ;;
        --skip-deps)
            SKIP_DEPS=true
            ;;
        --configure-runtime)
            CONFIGURE_RUNTIME=true
            ;;
        --remove-runtime-config)
            REMOVE_RUNTIME_CONFIG=true
            ;;
        --runtime-user)
            if [[ $# -lt 2 ]]; then
                echo "❌ --runtime-user requires a value"
                usage
                exit 1
            fi
            RUNTIME_USER="$2"
            shift
            ;;
        --for-bake)
            SKIP_KVM_CHECK=true
            SKIP_RUNTIME_CHECK=true
            ;;
        --skip-kvm-check)
            SKIP_KVM_CHECK=true
            ;;
        --skip-runtime-check)
            SKIP_RUNTIME_CHECK=true
            ;;
        --firecracker-version)
            if [[ $# -lt 2 ]]; then
                echo "❌ --firecracker-version needs a release tag such as 'v1.14.1'."
                exit 1
            fi
            FIRECRACKER_VERSION="$2"
            shift
            ;;
        --firecracker-dir)
            if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
                echo "❌ --firecracker-dir needs an absolute folder."
                exit 1
            fi
            FIRECRACKER_DIR="$2"
            FIRECRACKER_DIR_CONFIGURED=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
    shift
done

resolve_runtime_user() {
    if [[ -n "${RUNTIME_USER}" ]]; then
        echo "${RUNTIME_USER}"
        return
    fi

    if [[ -n "${SUDO_USER:-}" ]]; then
        echo "${SUDO_USER}"
        return
    fi

    if [[ -n "${USER:-}" && "${USER}" != "root" ]]; then
        echo "${USER}"
        return
    fi

    if command -v logname >/dev/null 2>&1; then
        local login_user
        login_user="$(logname 2>/dev/null || true)"
        if [[ -n "${login_user}" && "${login_user}" != "root" ]]; then
            echo "${login_user}"
            return
        fi
    fi

    echo ""
}

resolve_user_home() {
    local runtime_user="$1"
    local runtime_home=""
    if command -v getent >/dev/null 2>&1; then
        runtime_home="$(getent passwd "${runtime_user}" 2>/dev/null | cut -d: -f6 || true)"
    fi
    if [[ -z "${runtime_home}" && "${runtime_user}" == "$(id -un)" ]]; then
        runtime_home="${HOME:-}"
    fi
    if [[ -z "${runtime_home}" ]]; then
        echo "❌ Home folder for '${runtime_user}' was not found; rerun with '--firecracker-dir /absolute/path'." >&2
        return 1
    fi
    printf '%s\n' "${runtime_home}"
}

resolve_firecracker_directory() {
    local runtime_user
    local runtime_home
    runtime_user="$(resolve_runtime_user)"
    if [[ -z "${runtime_user}" && ${EUID} -eq 0 ]]; then
        runtime_user="root"
    fi
    if [[ -z "${runtime_user}" ]]; then
        echo "❌ SmolVM could not determine your account; rerun with '--runtime-user $(id -un)'."
        return 1
    fi
    runtime_home="$(resolve_user_home "${runtime_user}")"

    if [[ "${FIRECRACKER_DIR_CONFIGURED}" != "true" && -n "${SMOLVM_FIRECRACKER_DIR+x}" ]]; then
        if [[ -z "${SMOLVM_FIRECRACKER_DIR//[[:space:]]/}" ]]; then
            echo "❌ SMOLVM_FIRECRACKER_DIR is empty; unset it or set it to an absolute folder."
            return 1
        fi
        FIRECRACKER_DIR="${SMOLVM_FIRECRACKER_DIR}"
        FIRECRACKER_DIR_CONFIGURED=true
    fi
    if [[ -z "${FIRECRACKER_DIR}" ]]; then
        FIRECRACKER_DIR="${runtime_home}/.smolvm/bin"
    fi
    if [[ "${FIRECRACKER_DIR}" != /* ]]; then
        echo "❌ Firecracker folder must be absolute: '${FIRECRACKER_DIR}'; rerun with '--firecracker-dir /absolute/path'."
        return 1
    fi
    if [[ -e "${FIRECRACKER_DIR}" && ! -d "${FIRECRACKER_DIR}" ]]; then
        echo "❌ Firecracker folder is a file: '${FIRECRACKER_DIR}'; choose another folder with '--firecracker-dir'."
        return 1
    fi
}

find_firecracker_path() {
    local selected="${FIRECRACKER_DIR}/firecracker"
    if [[ "${FIRECRACKER_DIR_CONFIGURED}" == "true" ]]; then
        if [[ -f "${selected}" && -x "${selected}" ]]; then
            printf '%s\n' "${selected}"
        fi
        return 0
    fi

    local system_path
    system_path="$(command -v firecracker 2>/dev/null || true)"
    if [[ -n "${system_path}" ]]; then
        printf '%s\n' "${system_path}"
    elif [[ -f "${selected}" && -x "${selected}" ]]; then
        printf '%s\n' "${selected}"
    fi
    return 0
}

ensure_group_membership() {
    local group_name="$1"
    local hint_cmd="$2"
    local target_user
    target_user="$(resolve_runtime_user)"

    if [[ -z "${target_user}" ]]; then
        echo "⚠️  Could not determine target user for '${group_name}' group setup."
        echo "    Re-run with --runtime-user <user>, then run: ${hint_cmd}"
        return 0
    fi

    if ! id "${target_user}" >/dev/null 2>&1; then
        echo "⚠️  User '${target_user}' not found; skipping ${group_name} group setup."
        return 0
    fi

    if ! getent group "${group_name}" >/dev/null 2>&1; then
        echo "Creating ${group_name} group..."
        groupadd "${group_name}"
    fi

    if id -nG "${target_user}" | tr ' ' '\n' | grep -qx "${group_name}"; then
        echo "✅ User '${target_user}' is already in the ${group_name} group"
        return 0
    fi

    echo "Adding user '${target_user}' to ${group_name} group..."
    usermod -aG "${group_name}" "${target_user}"
    echo "✅ Added '${target_user}' to ${group_name} group"
    echo "   Run '${hint_cmd}' (or log out/in) before using ${group_name}-gated features."
}

ensure_docker_group_membership() {
    ensure_group_membership "docker" "newgrp docker"
}

ensure_kvm_group_membership() {
    ensure_group_membership "kvm" "newgrp kvm"
}

# Install a udev rule that pins /dev/kvm to mode 0660 + group=kvm and
# tags it for systemd-logind's `uaccess` ACL helper. The mode/group bits
# normalize behavior across distros (some leave /dev/kvm root-owned by
# default); the `uaccess` tag means a desktop user logged in at the
# active seat gets a per-user POSIX ACL on /dev/kvm without joining
# the kvm group at all. Headless SSH sessions still rely on the kvm
# group membership above (uaccess only fires for seat sessions), but
# the rule is harmless there and avoids a footgun if the host later
# grows a desktop session.
install_kvm_udev_rule() {
    local rule_path="/etc/udev/rules.d/65-smolvm-kvm.rules"
    local rule_body='# Managed by smolvm setup. Do not edit by hand.
KERNEL=="kvm", GROUP="kvm", MODE="0660", TAG+="uaccess"
'

    # `$(cat …)` strips trailing newlines, so re-append one before comparing
    # against rule_body (which ends with a newline) — otherwise this idempotency
    # check would never match and we'd re-write + re-trigger udev on every run.
    if [[ -f "${rule_path}" ]] && [[ "$(cat "${rule_path}")"$'\n' == "${rule_body}" ]]; then
        echo "✅ KVM udev rule already in place at ${rule_path}"
        return 0
    fi

    if ! command -v udevadm >/dev/null 2>&1; then
        echo "ℹ️  udevadm not found; skipping KVM udev rule install."
        echo "    /dev/kvm permissions will follow whatever the distro ships."
        return 0
    fi

    echo "Installing KVM udev rule at ${rule_path}..."
    install -d -m 0755 /etc/udev/rules.d
    printf '%s' "${rule_body}" > "${rule_path}"
    chmod 0644 "${rule_path}"

    if udevadm control --reload >/dev/null 2>&1; then
        # Apply immediately so the current session sees the new mode/group
        # without waiting for the next kernel device event.
        if udevadm trigger /dev/kvm >/dev/null 2>&1; then
            echo "✅ KVM udev rule installed and applied"
        else
            echo "⚠️  Wrote ${rule_path} but 'udevadm trigger /dev/kvm' failed."
            echo "    Run 'sudo udevadm trigger /dev/kvm' or reboot to apply."
        fi
    else
        echo "⚠️  Wrote ${rule_path} but 'udevadm control --reload' failed."
        echo "    The rule will take effect on next reboot."
    fi
}

run_runtime_config() {
    local mode="$1"
    local runtime_user
    runtime_user="$(resolve_runtime_user)"

    if [[ -z "${runtime_user}" ]]; then
        echo "❌ Runtime user is required for runtime sudoers. Pass --runtime-user <user>."
        return 1
    fi

    if [[ ! -x "${RUNTIME_CONFIG_SCRIPT}" ]]; then
        echo "❌ Runtime config helper not found or not executable: ${RUNTIME_CONFIG_SCRIPT}"
        return 1
    fi

    local configure_args=(--runtime-user "${runtime_user}")
    if [[ "${SKIP_RUNTIME_CHECK}" == "true" ]]; then
        configure_args+=(--skip-runtime-check)
    fi

    case "${mode}" in
        configure)
            bash "${RUNTIME_CONFIG_SCRIPT}" "${configure_args[@]}"
            ;;
        check)
            bash "${RUNTIME_CONFIG_SCRIPT}" --runtime-user "${runtime_user}" --check-only
            ;;
        remove)
            bash "${RUNTIME_CONFIG_SCRIPT}" --runtime-user "${runtime_user}" --remove
            ;;
        *)
            echo "❌ Internal error: unknown runtime config mode '${mode}'"
            return 1
            ;;
    esac
}

if [[ "${REMOVE_RUNTIME_CONFIG}" == "true" ]]; then
    run_runtime_config remove
    exit 0
fi

resolve_firecracker_directory

missing_items=()

check_kvm() {
    if [[ -e /dev/kvm ]]; then
        echo "  ✅ KVM device present (/dev/kvm)"
    elif [[ "${SKIP_KVM_CHECK}" == "true" ]]; then
        echo "  ℹ️ KVM device missing (/dev/kvm) — skipped (--skip-kvm-check / --for-bake)"
    else
        echo "  ❌ KVM device missing (/dev/kvm)"
        missing_items+=("KVM (/dev/kvm)")
    fi
}

check_cmd() {
    local cmd="$1"
    local label="$2"
    if command -v "$cmd" >/dev/null 2>&1; then
        echo "  ✅ ${label}"
    else
        echo "  ❌ ${label}"
        missing_items+=("${label}")
    fi
}

check_firecracker() {
    local path
    path="$(find_firecracker_path)"
    if [[ -n "${path}" ]]; then
        echo "  ✅ Firecracker (${path})"
    else
        echo "  ❌ Firecracker (${FIRECRACKER_DIR}/firecracker)"
        missing_items+=("Firecracker")
    fi
}

run_checks() {
    check_kvm
    check_cmd "ip" "ip (iproute2)"
    check_cmd "nft" "nft (nftables)"
    check_cmd "ssh" "ssh (openssh-client)"
    check_firecracker
    if [[ "${WITH_DOCKER}" == "true" ]]; then
        check_cmd "docker" "docker"
    fi
}

missing_commands=()

collect_missing_commands() {
    missing_commands=()
    local cmd
    for cmd in "$@"; do
        if ! command -v "${cmd}" >/dev/null 2>&1; then
            missing_commands+=("${cmd}")
        fi
    done
}

packages_for_commands() {
    local family="$1"
    shift
    local cmd
    local package
    local packages=()
    for cmd in "$@"; do
        package="${cmd}"
        case "${family}:${cmd}" in
            apt:ip) package="iproute2" ;;
            apt:nft) package="nftables" ;;
            apt:ssh) package="openssh-client" ;;
            apt:sysctl) package="procps" ;;
            apt:visudo) package="sudo" ;;
            apt:install) package="coreutils" ;;
            dnf:ip) package="iproute" ;;
            dnf:nft) package="nftables" ;;
            dnf:ssh) package="openssh-clients" ;;
            dnf:sysctl) package="procps-ng" ;;
            dnf:visudo) package="sudo" ;;
            dnf:install) package="coreutils" ;;
        esac
        packages+=("${package}")
    done
    printf '%s\n' "${packages[@]}"
}

install_missing_dependencies() {
    if [[ ${#missing_commands[@]} -eq 0 ]]; then
        echo "✅ Host dependencies already installed"
        return 0
    fi

    local packages=()
    if [[ -e /run/ostree-booted ]] && command -v rpm-ostree >/dev/null 2>&1; then
        mapfile -t packages < <(packages_for_commands dnf "${missing_commands[@]}")
        echo "❌ Required commands are missing: ${missing_commands[*]}; run 'sudo rpm-ostree install ${packages[*]}', reboot, then run 'smolvm setup --skip-deps'."
        return 1
    fi

    if command -v apt-get >/dev/null 2>&1; then
        mapfile -t packages < <(packages_for_commands apt "${missing_commands[@]}")
        if ! apt-get update -qq; then
            echo "⚠️  Package-list refresh failed; SmolVM will try the existing package list."
        fi
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}"
    elif command -v dnf >/dev/null 2>&1; then
        mapfile -t packages < <(packages_for_commands dnf "${missing_commands[@]}")
        dnf install -y "${packages[@]}"
    else
        echo "❌ Required commands are missing: ${missing_commands[*]}; install them with your operating system, then run 'smolvm setup --skip-deps'."
        return 1
    fi

    collect_missing_commands "${required_commands[@]}"
    if [[ ${#missing_commands[@]} -ne 0 ]]; then
        echo "❌ Required commands are still missing: ${missing_commands[*]}; install them, then run 'smolvm setup --skip-deps'."
        return 1
    fi
}

if [[ "${CHECK_ONLY}" == "true" ]]; then
    echo "=== SmolVM System Check ==="
    run_checks
    if [[ ${#missing_items[@]} -ne 0 ]]; then
        echo ""
        echo "❌ Missing prerequisites: ${missing_items[*]}"
        exit ${#missing_items[@]}
    fi

    if [[ "${CONFIGURE_RUNTIME}" == "true" ]]; then
        echo ""
        echo "Checking runtime sudoers configuration..."
        run_runtime_config check
    fi

    echo ""
    echo "✅ System ready"
    exit 0
fi

echo "=== SmolVM System Setup ==="

echo "Checking KVM..."
if [[ ! -e /dev/kvm ]]; then
    if [[ "${SKIP_KVM_CHECK}" == "true" ]]; then
        echo "ℹ️ /dev/kvm not present; skipping KVM check (--skip-kvm-check / --for-bake)."
        echo "   Run 'smolvm doctor' on the runtime host to verify KVM before booting VMs."
    else
        echo "❌ /dev/kvm not found. Enable KVM or nested virtualization."
        echo "   For bake-time installs on hosts without /dev/kvm, pass --for-bake."
        exit 1
    fi
fi
if [[ -e /dev/kvm ]]; then
    ensure_kvm_group_membership
    install_kvm_udev_rule
fi

firecracker_path="$(find_firecracker_path)"
required_commands=("ip" "nft" "ssh")
if [[ "${CONFIGURE_RUNTIME}" == "true" ]]; then
    required_commands+=("sysctl" "visudo" "install")
fi
if [[ -z "${firecracker_path}" ]]; then
    required_commands+=("wget" "tar")
fi
if [[ "${WITH_DOCKER}" == "true" ]]; then
    required_commands+=("curl")
fi
collect_missing_commands "${required_commands[@]}"

if [[ "${SKIP_DEPS}" == "true" ]]; then
    echo "Skipping operating-system package installation (--skip-deps)"
    if [[ ${#missing_commands[@]} -ne 0 ]]; then
        echo "❌ Required commands are missing: ${missing_commands[*]}; install them, then run 'smolvm setup --skip-deps'."
        exit 1
    fi
else
    install_missing_dependencies
fi

if [[ -n "${firecracker_path}" ]]; then
    echo "✅ Firecracker already installed: ${firecracker_path}"
else
    if [[ ! -f "${INSTALL_SCRIPT}" ]]; then
        echo "❌ Firecracker installer is missing: '${INSTALL_SCRIPT}'; reinstall SmolVM, then run 'smolvm setup' again."
        exit 1
    fi
    echo "Installing Firecracker in '${FIRECRACKER_DIR}'..."
    install_args=(--skip-deps --firecracker-dir "${FIRECRACKER_DIR}")
    runtime_user="$(resolve_runtime_user)"
    if [[ -n "${runtime_user}" ]]; then
        install_args+=(--runtime-user "${runtime_user}")
    fi
    if [[ -n "${FIRECRACKER_VERSION}" ]]; then
        install_args+=(--firecracker-version "${FIRECRACKER_VERSION}")
    fi
    bash "${INSTALL_SCRIPT}" "${install_args[@]}"
    firecracker_path="$(find_firecracker_path)"
fi

if [[ -z "${firecracker_path}" ]]; then
    echo "❌ Firecracker is missing from '${FIRECRACKER_DIR}'; run 'smolvm setup --firecracker-dir ${FIRECRACKER_DIR}' to install it."
    exit 1
fi

if [[ "${WITH_DOCKER}" == "true" ]]; then
    if command -v docker >/dev/null 2>&1; then
        echo "✅ Docker already installed"
    else
        if [[ -e /run/ostree-booted ]]; then
            echo "❌ Docker is not installed; install it through your operating system, or rerun 'smolvm setup' without '--with-docker'."
            exit 1
        fi
        if ! command -v curl >/dev/null 2>&1; then
            echo "❌ curl is missing; install it, then rerun 'smolvm setup --with-docker'."
            exit 1
        fi
        echo "Installing Docker..."
        curl -fsSL https://get.docker.com | sh
        if ! command -v docker >/dev/null 2>&1; then
            echo "❌ Docker install failed (docker command not found)."
            exit 1
        fi
    fi

    ensure_docker_group_membership
fi

if [[ "${CONFIGURE_RUNTIME}" == "true" ]]; then
    echo "Configuring runtime sudoers (no interactive password during SDK runtime)..."
    run_runtime_config configure
fi

echo "✅ System setup complete"
