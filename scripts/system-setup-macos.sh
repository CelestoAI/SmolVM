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
WITH_DOCKER=false
SKIP_DEPS=false

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Installs and checks macOS dependencies for SmolVM qemu backend.

Options:
  --check-only   Only validate prerequisites; do not install.
  --with-docker  Install Docker Desktop cask (optional; for image build workflows).
  --skip-deps    Skip Homebrew dependency installation (assumes qemu already present).
  -h, --help     Show this help.
EOF
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

    if [[ "$WITH_DOCKER" == "true" ]]; then
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

if ! find_qemu; then
    if ! command -v brew >/dev/null 2>&1; then
        echo "❌ Homebrew not found. Install from https://brew.sh and rerun."
        exit 1
    fi
    echo "Installing qemu via Homebrew..."
    brew install qemu
fi

if [[ "$SKIP_DEPS" == "true" ]]; then
    echo "Skipping optional dependency installation (--skip-deps)"
else
    if [[ "$WITH_DOCKER" == "true" ]] && ! command -v docker >/dev/null 2>&1; then
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
