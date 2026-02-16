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

# image-build-loopfs.sh - Privileged helper for SmolVM image-build loop mounts.
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
RUNTIME_USER="${SUDO_USER:-}"
RUNTIME_UID=""

if [[ -n "${RUNTIME_USER}" ]]; then
    RUNTIME_UID="$(id -u "${RUNTIME_USER}")"
fi

usage() {
    cat <<EOF
Usage:
  ${SCRIPT_NAME} mount <rootfs.ext4> <mount_dir>
  ${SCRIPT_NAME} extract <rootfs.tar> <mount_dir>
  ${SCRIPT_NAME} umount <mount_dir>
  ${SCRIPT_NAME} --help
EOF
}

die() {
    echo "❌ ${SCRIPT_NAME}: $1" >&2
    exit 1
}

resolve_abs_existing_path() {
    local input="$1"
    [[ "${input}" = /* ]] || die "path must be absolute: ${input}"
    readlink -f -- "${input}"
}

require_owned_by_runtime_user() {
    local path="$1"
    if [[ -z "${RUNTIME_UID}" ]]; then
        return 0
    fi

    local owner_uid
    owner_uid="$(stat -c %u -- "${path}")"
    if [[ "${owner_uid}" != "${RUNTIME_UID}" ]]; then
        die "path must be owned by runtime user '${RUNTIME_USER}': ${path}"
    fi
}

require_parent_owned_by_runtime_user() {
    local path="$1"
    local parent
    parent="$(dirname -- "${path}")"
    require_owned_by_runtime_user "${parent}"
}

mount_rootfs() {
    [[ $# -eq 2 ]] || die "mount requires: <rootfs.ext4> <mount_dir>"
    local rootfs_real
    local mount_real
    rootfs_real="$(resolve_abs_existing_path "$1")"
    mount_real="$(resolve_abs_existing_path "$2")"

    [[ -f "${rootfs_real}" ]] || die "rootfs is not a regular file: ${rootfs_real}"
    [[ "${rootfs_real}" == *.ext4 ]] || die "rootfs must end with .ext4: ${rootfs_real}"
    [[ -d "${mount_real}" ]] || die "mount dir does not exist: ${mount_real}"

    require_owned_by_runtime_user "${rootfs_real}"
    require_owned_by_runtime_user "${mount_real}"

    mount -o loop "${rootfs_real}" "${mount_real}"
}

extract_rootfs_tar() {
    [[ $# -eq 2 ]] || die "extract requires: <rootfs.tar> <mount_dir>"
    local tar_real
    local mount_real
    tar_real="$(resolve_abs_existing_path "$1")"
    mount_real="$(resolve_abs_existing_path "$2")"

    [[ -f "${tar_real}" ]] || die "tar path is not a regular file: ${tar_real}"
    [[ "${tar_real}" == *.tar ]] || die "tar path must end with .tar: ${tar_real}"
    [[ -d "${mount_real}" ]] || die "mount dir does not exist: ${mount_real}"

    require_owned_by_runtime_user "${tar_real}"
    require_parent_owned_by_runtime_user "${mount_real}"

    tar -xf "${tar_real}" -C "${mount_real}"
}

unmount_rootfs() {
    [[ $# -eq 1 ]] || die "umount requires: <mount_dir>"
    local mount_real
    mount_real="$(resolve_abs_existing_path "$1")"

    [[ -d "${mount_real}" ]] || die "mount dir does not exist: ${mount_real}"
    require_parent_owned_by_runtime_user "${mount_real}"

    umount "${mount_real}"
}

main() {
    if [[ $# -eq 0 ]]; then
        usage
        return 2
    fi

    case "$1" in
        --help|-h)
            usage
            ;;
        mount)
            shift
            mount_rootfs "$@"
            ;;
        extract)
            shift
            extract_rootfs_tar "$@"
            ;;
        umount)
            shift
            unmount_rootfs "$@"
            ;;
        *)
            usage
            return 2
            ;;
    esac
}

main "$@"
