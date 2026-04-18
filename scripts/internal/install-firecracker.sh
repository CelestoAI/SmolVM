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

# install-firecracker.sh - Internal helper to install Firecracker and Jailer.
# Intended to be called by scripts/system-setup.sh.
set -euo pipefail

WITH_IMAGES=false
SKIP_DEPS=false
REQUIRE_KVM=false
FC_VERSION_OVERRIDE=""

usage() {
    cat <<EOF_USAGE
Usage: $(basename "$0") [--with-images] [--skip-deps] [--require-kvm] [--firecracker-version <ver>]

Options:
  --with-images               Download kernel/rootfs images after install
  --skip-deps                 Skip apt dependency install (requires wget + tar)
  --require-kvm               Fail if /dev/kvm is missing (default: skip the check
                              so installs work on bake hosts; runtime/doctor catch it)
  --firecracker-version <ver> Pin Firecracker release tag (default: built-in or
                              \$SMOLVM_FIRECRACKER_VERSION / \$FC_VERSION env)
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
            if [[ $# -lt 2 ]]; then
                echo "❌ --firecracker-version requires a value"
                usage
                exit 1
            fi
            if [[ -z "$2" || "$2" == -* ]]; then
                echo "❌ --firecracker-version got an invalid value: '$2'"
                echo "   Expected a release tag like 'v1.14.1' (no leading dash, not empty)."
                exit 1
            fi
            if [[ ! "$2" =~ ^[A-Za-z0-9._-]+$ ]]; then
                echo "❌ --firecracker-version contains invalid characters: '$2'"
                echo "   Allowed: letters, digits, '.', '_', '-' (e.g. 'v1.14.1')."
                exit 1
            fi
            FC_VERSION_OVERRIDE="$2"
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

SUDO=""
if [[ ${EUID} -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo "❌ sudo not found. Run as root or install sudo."
        exit 1
    fi
fi

run_root() {
    if [[ -n "$SUDO" ]]; then
        sudo "$@"
    else
        "$@"
    fi
}

echo "=== Installing Firecracker ==="

# Version precedence: --firecracker-version flag > SMOLVM_FIRECRACKER_VERSION > FC_VERSION (legacy) > default.
if [[ -n "${FC_VERSION_OVERRIDE}" ]]; then
    FC_VERSION="${FC_VERSION_OVERRIDE}"
elif [[ -n "${SMOLVM_FIRECRACKER_VERSION:-}" ]]; then
    FC_VERSION="${SMOLVM_FIRECRACKER_VERSION}"
else
    FC_VERSION="${FC_VERSION:-v1.14.1}"
fi

if [[ ! "${FC_VERSION}" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "❌ Firecracker version '${FC_VERSION}' is not a valid release tag."
    echo "   Allowed: letters, digits, '.', '_', '-' (e.g. 'v1.14.1')."
    exit 1
fi

ARCH=$(uname -m)

if [[ "${REQUIRE_KVM}" == "true" && ! -e /dev/kvm ]]; then
    echo "ERROR: /dev/kvm not found. KVM is required."
    exit 1
fi

if [[ "$SKIP_DEPS" == "true" ]]; then
    for cmd in wget tar; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            echo "❌ Missing required command: $cmd (install it or rerun without --skip-deps)"
            exit 1
        fi
    done
else
    run_root apt-get update -qq
    run_root apt-get install -y curl wget jq nftables e2fsprogs -qq
fi

cd /tmp
wget -q "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz"
tar -xzf "firecracker-${FC_VERSION}-${ARCH}.tgz"

run_root cp "release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" /usr/local/bin/firecracker
run_root cp "release-${FC_VERSION}-${ARCH}/jailer-${FC_VERSION}-${ARCH}" /usr/local/bin/jailer
run_root chmod +x /usr/local/bin/firecracker /usr/local/bin/jailer

run_root groupadd -g 2000 firecracker 2>/dev/null || true
run_root useradd -u 2000 -g firecracker -s /bin/false -d /srv/jailer firecracker 2>/dev/null || true
run_root mkdir -p /srv/jailer
run_root chown firecracker:firecracker /srv/jailer

rm -rf "/tmp/release-${FC_VERSION}-${ARCH}" "/tmp/firecracker-${FC_VERSION}-${ARCH}.tgz"

echo ""
echo "✅ Firecracker Installation Complete"
echo "   Firecracker: $(firecracker --version 2>&1 | head -1)"
echo "   Jailer: $(jailer --version 2>&1 | head -1)"

if [[ "$WITH_IMAGES" == "true" ]]; then
    echo ""
    bash "$(cd "$(dirname "$0")/.." && pwd)/download-images.sh"
fi
