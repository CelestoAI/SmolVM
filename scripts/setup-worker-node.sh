#!/usr/bin/env bash
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

# setup-worker-node.sh — Host-level security hardening for SmolVM worker nodes.
#
# PURPOSE
# -------
# This script applies kernel and OS settings that are required BEFORE starting
# the SmolVM reconciler on a bare-metal or nested-virt worker node.  These are
# host-level invariants that no amount of application code can compensate for
# if they are wrong (C2: Defence in Depth).
#
# Every setting written here is verified at reconciler startup by
# `smolvm.doctor.check_worker_node_security()`.  If any check fails the
# reconciler refuses to start rather than run in a degraded security posture.
#
# SETTINGS APPLIED
# ----------------
#  1. Swap disabled permanently (C1: no secret remanence on host disk)
#  2. KSM disabled (cross-VM memory-page timing sidechannel)
#  3. Transparent HugePages disabled (THP causes latency spikes)
#  4. kvm module loaded with nx_huge_pages=never (CVE-2021-3737 mitigation)
#  5. /dev/kvm ownership/permissions set for the kvm group
#  6. Docker coexistence: DOCKER-USER rules to allow tap+ forwarding (only if
#     Docker is installed; no-op otherwise)
#
# USAGE
# -----
#   sudo ./scripts/setup-worker-node.sh [--check-only]
#
# OPTIONS
#   --check-only          Validate settings without applying changes.
#   -h, --help            Show this help.
#
# EXIT CODES
#   0  All settings already correct (idempotent).
#   1  One or more settings could not be applied / verified.

set -euo pipefail
ORIG_ARGS=("$@")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
readonly SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
readonly KVM_DEV="/dev/kvm"
readonly KSM_RUN="/sys/kernel/mm/ksm/run"
readonly THP_ENABLED="/sys/kernel/mm/transparent_hugepage/enabled"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
CHECK_ONLY=false

usage() {
    cat <<EOF_USAGE
Usage: ${SCRIPT_NAME} [--check-only] [-h|--help]

Apply host-level security hardening required for SmolVM worker nodes.
Run as root (script will re-exec under sudo if available).

Options:
  --check-only           Only validate; do not apply any changes.
  -h, --help             Show this help message.
EOF_USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check-only)  CHECK_ONLY=true ;;
        -h|--help)     usage; exit 0 ;;
        *)
            echo "Unknown argument: $1" >&2
            usage; exit 1 ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Privilege check — re-exec under sudo if not already root
# ---------------------------------------------------------------------------
if [[ ${EUID} -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo -E "$0" "${ORIG_ARGS[@]}"
    fi
    echo "❌ This script must be run as root (sudo not found)." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
FAILURES=0

pass()  { echo "  ✅  $*"; }
fail()  { echo "  ❌  $*" >&2; FAILURES=$((FAILURES + 1)); }
info()  { echo "  ℹ️   $*"; }
warn()  { echo "  ⚠️   $*"; }

# ---------------------------------------------------------------------------
# Helper: apply or check a sysctl-style setting via a sysfs file.
# Usage: apply_sysfs <path> <expected_value> <description>
# ---------------------------------------------------------------------------
apply_sysfs() {
    local path="$1"
    local expected="$2"
    local description="$3"

    if [[ ! -e "${path}" ]]; then
        # Not a hard failure on every kernel build (e.g. KSM may be absent).
        warn "${description}: ${path} does not exist — skipping"
        return
    fi

    local current
    current="$(cat "${path}" | awk '{print $1}')"  # awk strips enabled-marker brackets like [never]

    if [[ "${current}" == "${expected}" ]]; then
        pass "${description}: already '${expected}'"
        return
    fi

    if [[ "${CHECK_ONLY}" == "true" ]]; then
        fail "${description}: expected '${expected}', got '${current}' (${path})"
        return
    fi

    echo "${expected}" > "${path}" 2>/dev/null || {
        fail "${description}: could not write '${expected}' to ${path}"
        return
    }

    local after
    after="$(cat "${path}" | awk '{print $1}')"
    if [[ "${after}" == "${expected}" ]]; then
        pass "${description}: set to '${expected}'"
    else
        fail "${description}: write appeared to succeed but value is now '${after}'"
    fi
}

# ---------------------------------------------------------------------------
# helper: read the current value of a THP enablement file (strips brackets)
# e.g. "always madvise [never]" → "never"
# ---------------------------------------------------------------------------
read_thp_value() {
    local path="$1"
    # Extract bracketed value, e.g., "always madvise [never]" → "never"
    # Fallback natively outputs the whole string if brackets are not found.
    sed -n 's/.*\[\([^]]*\)\].*/\1/p' "${path}" 2>/dev/null | head -n1
}

check_thp() {
    local description="Transparent HugePages disabled (latency / timing)"

    if [[ ! -e "${THP_ENABLED}" ]]; then
        warn "THP: ${THP_ENABLED} does not exist — skipping"
        return
    fi

    local current
    current="$(read_thp_value "${THP_ENABLED}")"

    if [[ "${current}" == "never" ]]; then
        pass "${description}: already 'never'"
        return
    fi

    if [[ "${CHECK_ONLY}" == "true" ]]; then
        fail "${description}: expected 'never', got '${current}'"
        return
    fi

    echo "never" > "${THP_ENABLED}" 2>/dev/null || {
        fail "${description}: could not write 'never' to ${THP_ENABLED}"
        return
    }

    local after
    after="$(read_thp_value "${THP_ENABLED}")"
    if [[ "${after}" == "never" ]]; then
        pass "${description}: set to 'never'"
    else
        fail "${description}: write appeared to succeed but value is '${after}'"
    fi
}

# ---------------------------------------------------------------------------
# 1. Swap — disable permanently
# ---------------------------------------------------------------------------
check_or_apply_swap() {
    local description="Swap disabled (C1: no secret remanence on disk)"

    # Runtime check: is any swap currently active?
    local swap_total
    swap_total="$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo "0")"

    if [[ "${swap_total}" -ne 0 ]]; then
        if [[ "${CHECK_ONLY}" == "true" ]]; then
            fail "${description}: swap is active (${swap_total} kB)"
            return
        fi
        swapoff -a || { fail "${description}: swapoff -a failed"; return; }
        info "Swap deactivated for this boot"
    fi

    # Re-read after potential swapoff
    swap_total="$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo "0")"
    if [[ "${swap_total}" -ne 0 ]]; then
        fail "${description}: swapoff ran but swap is still reported active"
        return
    fi

    # Persistence: remove swap entries from /etc/fstab
    if awk '!/^#/ && $3 == "swap" {found=1; exit} END {exit !found}' /etc/fstab 2>/dev/null; then
        if [[ "${CHECK_ONLY}" == "true" ]]; then
            fail "${description}: /etc/fstab still contains swap entries"
            return
        fi
        # Back up fstab, then strip swap lines using awk (checking column 3 for 'swap' fstype)
        cp /etc/fstab /etc/fstab.bak.smolvm-worker
        awk '!/^#/ && $3 == "swap" {next} {print}' /etc/fstab.bak.smolvm-worker > /etc/fstab
        info "/etc/fstab swap entries removed (backup: /etc/fstab.bak.smolvm-worker)"
    fi

    # Final persistence check
    if awk '!/^#/ && $3 == "swap" {found=1; exit} END {exit !found}' /etc/fstab 2>/dev/null; then
        fail "${description}: /etc/fstab still contains swap entries after awk"
    else
        pass "${description}"
    fi
}

# ---------------------------------------------------------------------------
# 2. KSM — Kernel Samepage Merging off (cross-VM timing sidechannel)
# ---------------------------------------------------------------------------
check_or_apply_ksm() {
    apply_sysfs "${KSM_RUN}" "0" "KSM disabled (cross-VM memory timing sidechannel)"
}

# ---------------------------------------------------------------------------
# 3. THP — Transparent HugePages never (latency spikes)
# ---------------------------------------------------------------------------
check_or_apply_thp() {
    check_thp
}

# ---------------------------------------------------------------------------
# 4. KVM module — nx_huge_pages=never (CVE-2021-3737 / KVM iTLB multihit)
# ---------------------------------------------------------------------------
check_or_apply_kvm_nx() {
    local param_path="/sys/module/kvm/parameters/nx_huge_pages"
    local description="kvm nx_huge_pages=never (CVE-2021-3737 mitigation)"

    if [[ ! -e "${param_path}" ]]; then
        # KVM module may not be loaded yet; attempt modprobe.
        if [[ "${CHECK_ONLY}" == "true" ]]; then
            fail "${description}: ${param_path} not present (kvm module not loaded?)"
            return
        fi
        modprobe kvm nx_huge_pages=never 2>/dev/null || {
            fail "${description}: modprobe kvm nx_huge_pages=never failed"
            return
        }
    fi

    if [[ ! -e "${param_path}" ]]; then
        fail "${description}: ${param_path} still absent after modprobe"
        return
    fi

    local current
    current="$(cat "${param_path}" | tr '[:upper:]' '[:lower:]')"

    # Acceptable values: "never" or "N" (some kernels use the letter form).
    if [[ "${current}" == "never" || "${current}" == "n" ]]; then
        pass "${description}: nx_huge_pages='${current}'"
        return
    fi

    if [[ "${CHECK_ONLY}" == "true" ]]; then
        fail "${description}: nx_huge_pages='${current}' (expected 'never' / 'N')"
        return
    fi

    # The parameter is read-only once the module is loaded; we can only reload.
    # Reloading kvm is disruptive on a live node — document and fail.
    # Try to detect which kvm vendor module is loaded
    local kvm_mod="kvm_intel"
    if [ -d "/sys/module/kvm_amd" ]; then
        kvm_mod="kvm_amd"
    fi
    fail "${description}: nx_huge_pages='${current}'. KVM module must be reloaded to apply changes. Try: rmmod ${kvm_mod} kvm && modprobe kvm nx_huge_pages=never && modprobe ${kvm_mod}"
}

# ---------------------------------------------------------------------------
# 5. /dev/kvm permissions — 660 + kvm group ownership
# ---------------------------------------------------------------------------
check_or_apply_kvm_perms() {
    local kvm_group="kvm"
    local description="/dev/kvm permissions (660, group ${kvm_group})"

    if [[ ! -e "${KVM_DEV}" ]]; then
        fail "${description}: ${KVM_DEV} not found (KVM unavailable)"
        return
    fi

    # Ensure the kvm group exists
    if ! getent group "${kvm_group}" >/dev/null 2>&1; then
        if [[ "${CHECK_ONLY}" == "true" ]]; then
            fail "${description}: group '${kvm_group}' does not exist"
            return
        fi
        groupadd "${kvm_group}"
        info "Created group '${kvm_group}'"
    fi

    local current_perms current_group
    current_perms="$(stat -c '%a' "${KVM_DEV}")"
    current_group="$(stat -c '%G' "${KVM_DEV}")"

    if [[ "${current_perms}" == "660" && "${current_group}" == "${kvm_group}" ]]; then
        pass "${description}: correct (${KVM_DEV} is 660 ${kvm_group})"
        return
    fi

    if [[ "${CHECK_ONLY}" == "true" ]]; then
        fail "${description}: ${KVM_DEV} is ${current_perms} ${current_group} (want 660 ${kvm_group})"
        return
    fi

    chmod 660 "${KVM_DEV}" && chgrp "${kvm_group}" "${KVM_DEV}" || {
        fail "${description}: chmod/chgrp failed on ${KVM_DEV}"
        return
    }

    pass "${description}: applied (${KVM_DEV} is now 660 ${kvm_group})"
}

# ---------------------------------------------------------------------------
# 6. Docker coexistence — allow SmolVM tap traffic through Docker's firewall
# ---------------------------------------------------------------------------
# Docker's daemon (when --iptables=true, the default) restricts the global
# FORWARD chain to protect its own containers.  This blocks SmolVM's tap
# traffic.  Docker provides the DOCKER-USER chain specifically for user rules
# that are evaluated before Docker's own DROP logic.  These two rules let
# SmolVM tap interfaces route normally while leaving Docker container
# isolation completely intact.
#
# Only applied when Docker is present.  Idempotent: -C checks before -I.
# ---------------------------------------------------------------------------
check_or_apply_docker_coexistence() {
    local description="Docker coexistence (tap+ forwarding via DOCKER-USER)"

    # Skip entirely if Docker is not installed — nothing to do.
    if ! command -v docker > /dev/null 2>&1; then
        warn "${description}: docker not found — skipping (not needed)"
        return
    fi

    # Verify iptables is available (required to inspect/add DOCKER-USER rules).
    if ! command -v iptables > /dev/null 2>&1; then
        fail "${description}: iptables not found but Docker is installed"
        return
    fi

    # Check whether the DOCKER-USER chain exists (Docker creates it on first
    # start; if Docker was never started it may be absent).
    if ! iptables -n -L DOCKER-USER >/dev/null 2>&1; then
        if [[ "${CHECK_ONLY}" == "true" ]]; then
            fail "${description}: DOCKER-USER chain is missing."
        else
            fail "${description}: DOCKER-USER chain is missing. Cannot apply Docker coexistence hardening."
        fi
        return
    fi

    local missing=0

    # Rule 1: allow new outbound traffic from any tap interface.
    if ! iptables -C DOCKER-USER -i tap+ -j ACCEPT > /dev/null 2>&1; then
        missing=$((missing + 1))
        if [[ "${CHECK_ONLY}" != "true" ]]; then
            iptables -I DOCKER-USER -i tap+ -j ACCEPT || {
                fail "${description}: could not insert outbound tap+ rule"
                return
            }
        fi
    fi

    # Rule 2: allow established/related return traffic back to tap interfaces.
    if ! iptables -C DOCKER-USER -o tap+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT > /dev/null 2>&1; then
        missing=$((missing + 1))
        if [[ "${CHECK_ONLY}" != "true" ]]; then
            iptables -I DOCKER-USER -o tap+ -m conntrack \
                --ctstate RELATED,ESTABLISHED -j ACCEPT || {
                fail "${description}: could not insert return-traffic tap+ rule"
                return
            }
        fi
    fi

    if [[ "${CHECK_ONLY}" == "true" && ${missing} -gt 0 ]]; then
        fail "${description}: ${missing} DOCKER-USER rule(s) missing (run without --check-only to apply)"
        return
    fi

    if [[ ${missing} -gt 0 ]]; then
        pass "${description}: ${missing} rule(s) applied"
        info "Rules are in kernel memory only. To persist across reboots:"
        info "  apt install iptables-persistent && netfilter-persistent save"
    else
        pass "${description}: DOCKER-USER rules already present"
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
echo "=== SmolVM Worker Node Hardening ==="
if [[ "${CHECK_ONLY}" == "true" ]]; then
    echo "(check-only mode — no changes will be made)"
fi
echo ""

check_or_apply_swap
check_or_apply_ksm
check_or_apply_thp
check_or_apply_kvm_nx
check_or_apply_kvm_perms
check_or_apply_docker_coexistence

echo ""
if [[ ${FAILURES} -eq 0 ]]; then
    echo "✅ Worker node hardening complete — all checks passed"
    exit 0
else
    echo "❌ ${FAILURES} check(s) failed — reconciler should NOT be started"
    exit 1
fi
