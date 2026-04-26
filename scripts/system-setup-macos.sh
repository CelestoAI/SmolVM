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

# system-setup-macos.sh - macOS setup for SmolVM qemu backend.
set -euo pipefail

CHECK_ONLY=false
WITH_PODMAN=false
WITH_DOCKER_CLASSIC=false
SKIP_DEPS=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Installs and checks macOS dependencies for SmolVM qemu backend.

Options:
  --check-only           Only validate prerequisites; do not install.
  --with-podman          Install Podman + start its Linux VM (recommended) for image builds.
  --with-docker          Alias for --with-podman (legacy flag name).
  --with-docker-classic  Install Docker Desktop cask (legacy; only if you need Docker).
  --skip-deps            Skip Homebrew dependency installation (assumes qemu already present).
  -h, --help             Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check-only)
            CHECK_ONLY=true
            ;;
        --with-podman)
            WITH_PODMAN=true
            ;;
        --with-docker)
            echo "ℹ️  --with-docker now installs Podman (lighter than Docker Desktop)."
            echo "    Pass --with-docker-classic if you specifically need Docker Desktop."
            WITH_PODMAN=true
            ;;
        --with-docker-classic)
            WITH_DOCKER_CLASSIC=true
            ;;
        --skip-deps)
            SKIP_DEPS=true
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

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "❌ This script is for macOS only."
    exit 1
fi

# Homebrew is only required when we may install dependencies.
if [[ "$CHECK_ONLY" != "true" && "$SKIP_DEPS" != "true" ]]; then
    if ! command -v brew >/dev/null 2>&1; then
        echo "❌ Homebrew not found. Install from https://brew.sh and rerun."
        exit 1
    fi
fi

find_qemu() {
    command -v qemu-system-aarch64 >/dev/null 2>&1 && return 0
    command -v qemu-system-x86_64 >/dev/null 2>&1 && return 0
    return 1
}

check_prereqs() {
    local missing=0

    if find_qemu; then
        echo "✅ qemu-system binary found"
    else
        echo "❌ qemu-system binary missing"
        missing=1
    fi

    if command -v ssh >/dev/null 2>&1; then
        echo "✅ ssh found"
    else
        echo "❌ ssh missing"
        missing=1
    fi

    if [[ "$WITH_PODMAN" == "true" ]]; then
        if ! command -v podman >/dev/null 2>&1; then
            echo "⚠️  podman not found"
            echo "    Fix: 'brew install podman && podman machine init && podman machine start'"
            missing=1
        elif ! podman machine inspect podman-machine-default >/dev/null 2>&1; then
            echo "⚠️  podman is installed but no Linux VM has been created"
            echo "    Fix: 'podman machine init' then 'podman machine start'"
            missing=1
        elif ! podman machine inspect podman-machine-default --format '{{.State}}' 2>/dev/null | grep -q running; then
            echo "⚠️  podman is installed but its Linux VM is not running"
            echo "    Fix: 'podman machine start'"
            missing=1
        else
            echo "✅ podman ready (Linux VM running)"
        fi
    fi

    if [[ "$WITH_DOCKER_CLASSIC" == "true" ]]; then
        if command -v docker >/dev/null 2>&1; then
            echo "✅ docker found"
        else
            echo "⚠️  docker not found"
            missing=1
        fi
    fi

    return $missing
}

if [[ "$CHECK_ONLY" == "true" ]]; then
    echo "=== SmolVM macOS check ==="
    if check_prereqs; then
        echo "✅ macOS prerequisites look good"
        exit 0
    fi
    echo "❌ macOS prerequisites missing"
    exit 1
fi

echo "=== SmolVM macOS setup (qemu backend) ==="

if [[ "$SKIP_DEPS" == "true" ]]; then
    echo "Skipping dependency installation (--skip-deps)"
else
    if ! find_qemu; then
        echo "Installing qemu via Homebrew..."
        brew install qemu
    fi

    if [[ "$WITH_PODMAN" == "true" ]]; then
        if ! command -v podman >/dev/null 2>&1; then
            echo "Installing Podman via Homebrew..."
            brew install podman
        fi
        if ! podman machine inspect podman-machine-default >/dev/null 2>&1; then
            echo "Initialising Podman's Linux VM (first run can take ~30 seconds)..."
            podman machine init
        fi
        if ! podman machine inspect podman-machine-default --format '{{.State}}' 2>/dev/null | grep -q running; then
            echo "Starting Podman's Linux VM..."
            podman machine start
        fi
        echo "ℹ️  Podman is ready. Verify with 'smolvm doctor'."
    fi

    if [[ "$WITH_DOCKER_CLASSIC" == "true" ]] && ! command -v docker >/dev/null 2>&1; then
        echo "Installing Docker Desktop cask via Homebrew..."
        brew install --cask docker
    fi
fi

echo ""
if check_prereqs; then
    echo "✅ macOS setup complete"
else
    echo "❌ Setup incomplete"
    exit 1
fi
