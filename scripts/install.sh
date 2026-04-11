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

# install.sh - One-command installer for SmolVM.
#
# Usage:
#   curl -sSL https://celesto.ai/install.sh | bash
#   curl -sSL https://celesto.ai/install.sh | bash -s -- --with-docker
#   curl -sSL https://celesto.ai/install.sh | bash -s -- --skip-deps
#
# What it does:
#   1. Installs uv (Python package manager) if not present
#   2. Installs smolvm into an isolated tool environment via uv
#   3. Runs `smolvm setup` to configure the host
#
# Options (forwarded to `smolvm setup`):
#   --skip-deps      Skip apt dependency installation (assumes deps are present)
#   --with-docker    Also install Docker for SSH image support
#
# After installation, the `smolvm` command is available globally.

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
RESET="\033[0m"

info()  { printf "${BOLD}${GREEN}==>${RESET} ${BOLD}%s${RESET}\n" "$*"; }
warn()  { printf "${BOLD}${YELLOW}warning:${RESET} %s\n" "$*"; }
error() { printf "${BOLD}${RED}error:${RESET} %s\n" "$*" >&2; }
die()   { error "$@"; exit 1; }

# Collect extra flags to forward to `smolvm setup`
SETUP_ARGS=()
for arg in "$@"; do
    SETUP_ARGS+=("$arg")
done

# ---------------------------------------------------------------------------
# Step 1 — Ensure uv is available
# ---------------------------------------------------------------------------

find_uv() {
    # 1. Already on PATH
    if command -v uv >/dev/null 2>&1; then
        return 0
    fi
    # 2. Common install locations (not yet on PATH in this session)
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            export PATH="$(dirname "$candidate"):$PATH"
            return 0
        fi
    done
    return 1
}

ensure_uv() {
    if find_uv; then
        info "uv is already installed ($(uv --version))"
        return
    fi

    info "Installing uv …"
    curl -LsSf https://astral.sh/uv/install.sh | sh

    # The installer puts uv in ~/.local/bin (or ~/.cargo/bin on older versions)
    if ! find_uv; then
        die "uv installation failed. Please install it manually: https://docs.astral.sh/uv/getting-started/installation/"
    fi

    info "uv installed ($(uv --version))"
}

# ---------------------------------------------------------------------------
# Step 2 — Install smolvm
# ---------------------------------------------------------------------------

install_smolvm() {
    if uv tool list 2>/dev/null | grep -q '^smolvm '; then
        # Installed as a uv tool — upgrade in place
        info "smolvm is already installed (uv tool), upgrading …"
        uv tool upgrade smolvm
    else
        # Fresh install (or installed via pip/editable — uv tool install won't conflict)
        info "Installing smolvm …"
        uv tool install smolvm
    fi

    # uv tool bin dir may not be on PATH yet in this session
    local tool_bin
    tool_bin="$(uv tool dir 2>/dev/null)/../bin"
    if [ -d "$tool_bin" ]; then
        export PATH="$tool_bin:$PATH"
    fi
    export PATH="$HOME/.local/bin:$PATH"

    if ! command -v smolvm >/dev/null 2>&1; then
        die "smolvm installation failed — 'smolvm' command not found on PATH."
    fi

    info "$(smolvm --version)"
}

# ---------------------------------------------------------------------------
# Step 3 — Run smolvm setup
# ---------------------------------------------------------------------------

run_setup() {
    info "Running smolvm setup …"
    smolvm setup --skip-deps ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}
}

# ---------------------------------------------------------------------------
# Step 4 — Shell PATH reminder
# ---------------------------------------------------------------------------

shell_hint() {
    # Check if ~/.local/bin is already on the user's default PATH
    local shell_name
    shell_name="$(basename "${SHELL:-/bin/sh}")"
    local rc_file=""
    case "$shell_name" in
        zsh)  rc_file="$HOME/.zshrc" ;;
        bash) rc_file="$HOME/.bashrc" ;;
        fish) rc_file="$HOME/.config/fish/config.fish" ;;
    esac

    if [ -n "$rc_file" ] && [ -f "$rc_file" ]; then
        if ! grep -q '.local/bin' "$rc_file" 2>/dev/null; then
            warn "Add ~/.local/bin to your PATH so 'smolvm' is available in new shells:"
            printf "\n  %s\n\n" "echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> $rc_file"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    printf "\n"
    printf "${GREEN}"
    cat <<'BANNER'
      ___      _        _          _   ___
     / __|___ | |___ __| |_ ___   /_\ |_ _|
    | (__/ -_)| / -_|_-<  _/ _ \ / _ \ | |
     \___\___||_\___/__/\__\___//_/ \_\___|
BANNER
    printf "${RESET}"
    printf "    ${BOLD}SmolVM Installer${RESET}\n"
    printf "    One command to give AI agents their own computer.\n\n"

    ensure_uv
    install_smolvm
    run_setup
    shell_hint

    printf "\n"
    info "Verifying installation …"
    smolvm doctor
    printf "\n"
    info "Done! SmolVM is ready to use."
    printf "\n"
}

main
