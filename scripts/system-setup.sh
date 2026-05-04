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
# Installs Firecracker/Jailer and host dependencies. Docker is optional.
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

usage() {
    cat <<EOF_USAGE
Usage: $(basename "$0") [options]

Installs host dependencies and Firecracker (no Python/venv involvement).

Options:
  --check-only                   Only validate system prerequisites; do not install.
  --with-docker                  Install Docker (required for SSH image demo).
  --skip-deps                    Skip apt dependency install (assumes deps already present).
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
                echo "❌ --firecracker-version requires a value"
                usage
                exit 1
            fi
            FIRECRACKER_VERSION="$2"
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

run_checks() {
    check_kvm
    check_cmd "ip" "ip (iproute2)"
    check_cmd "nft" "nft (nftables)"
    check_cmd "ssh" "ssh (openssh-client)"
    check_cmd "firecracker" "firecracker"
    if [[ "${WITH_DOCKER}" == "true" ]]; then
        check_cmd "docker" "docker"
    fi
}

required_runtime_cmds=("ip" "nft" "ssh")
required_install_cmds=("wget" "tar")
if [[ "${WITH_DOCKER}" == "true" ]]; then
    required_install_cmds+=("curl")
fi

check_required_cmds() {
    local missing=()
    for cmd in "$@"; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            missing+=("$cmd")
        fi
    done
    if [[ ${#missing[@]} -ne 0 ]]; then
        echo "❌ Missing required commands: ${missing[*]}"
        return 1
    fi
    return 0
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

if [[ "${SKIP_DEPS}" == "true" ]]; then
    echo "Skipping dependency installation (--skip-deps)"
    if ! check_required_cmds "${required_runtime_cmds[@]}" "${required_install_cmds[@]}"; then
        echo "Install missing commands or rerun without --skip-deps."
        exit 1
    fi
else
    echo "Installing host dependencies..."
    if ! command -v apt-get >/dev/null 2>&1; then
        echo "❌ apt-get not found. Install dependencies manually or rerun with --skip-deps."
        exit 1
    fi

    update_output=""
    if ! update_output=$(apt-get update -qq 2>&1); then
        echo "⚠️  apt-get update failed. Continuing with existing package lists."
        echo "    If installs fail, fix apt sources or rerun with --skip-deps."
    fi
    if [[ -n "${update_output}" ]]; then
        echo "${update_output}"
        if echo "${update_output}" | grep -Eq "EXPKEYSIG|NO_PUBKEY|The following signatures were invalid|Failed to fetch|^W:"; then
            echo "⚠️  apt-get update reported repository warnings."
            echo "    If installs fail, fix /etc/apt/sources.list(.d) or rerun with --skip-deps."
        fi
    fi

    if ! DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl wget jq nftables iproute2 e2fsprogs openssh-client tar; then
        echo "❌ apt-get install failed. Fix apt sources or install deps manually, then rerun with --skip-deps."
        exit 1
    fi

    if ! check_required_cmds "${required_runtime_cmds[@]}" "${required_install_cmds[@]}"; then
        echo "❌ Required commands are still missing after install."
        exit 1
    fi
fi

if command -v firecracker >/dev/null 2>&1; then
    echo "✅ Firecracker already installed: $(command -v firecracker)"
else
    if [[ ! -f "${INSTALL_SCRIPT}" ]]; then
        echo "❌ install.sh not found at ${INSTALL_SCRIPT}"
        exit 1
    fi
    echo "Installing Firecracker..."
    install_args=(--skip-deps)
    if [[ -n "${FIRECRACKER_VERSION}" ]]; then
        install_args+=(--firecracker-version "${FIRECRACKER_VERSION}")
    fi
    bash "${INSTALL_SCRIPT}" "${install_args[@]}"
fi

if ! command -v firecracker >/dev/null 2>&1; then
    echo "❌ Firecracker install failed: firecracker not found in PATH"
    exit 1
fi

if [[ "${WITH_DOCKER}" == "true" ]]; then
    if command -v docker >/dev/null 2>&1; then
        echo "✅ Docker already installed"
    else
        if ! command -v curl >/dev/null 2>&1; then
            echo "❌ curl not found (required for Docker install). Install curl or rerun without --skip-deps."
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
