#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)

# A macOS linker cannot directly produce SmolVM's static Linux guest binary.
# Use the release asset pinned and checksum-verified by this source checkout.
if [ "$(uname -s)" = "Darwin" ] && [ -z "${SMOLVM_GUEST_AGENT_BINARY:-}" ]; then
  guest_agent_binary=$(uv run --project "$repo_root" python -c \
    'from smolvm.images.builder import _download_guest_agent_binary; print(_download_guest_agent_binary())')
  export SMOLVM_GUEST_AGENT_BINARY="$guest_agent_binary"
fi

exec uv run --project "$repo_root" smolvm "$@"
