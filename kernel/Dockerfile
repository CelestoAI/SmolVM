# SmolVM guest-kernel build environment.
#
# Produces a reproducible toolchain for building the SmolVM Linux kernel.
# The image is arch-neutral — pass the target architecture to build.sh.
#
# Build:   docker build -t smolvm-kernel-builder kernel/
# Run:     docker run --rm -v "$PWD/kernel:/work" smolvm-kernel-builder \
#            /work/scripts/build.sh <x86_64|aarch64>

FROM alpine:3.20.3

# Build dependencies. Pinned to the Alpine 3.20 repo via the base image
# tag above — that's the coarse version contract for now.
#
# TODO(kernel/hardening): once the first `kernel-v1.0.0` release builds
# cleanly in CI, capture exact versions via `apk info -v` and re-pin
# package-by-package for fully reproducible builds.
RUN apk add --no-cache \
        bash \
        bc \
        bison \
        build-base \
        coreutils \
        diffutils \
        elfutils-dev \
        findutils \
        flex \
        gawk \
        grep \
        linux-headers \
        ncurses-dev \
        openssl-dev \
        perl \
        sed \
        tar \
        wget \
        xz \
        zstd

# Deterministic build metadata. SOURCE_DATE_EPOCH and the KBUILD_*
# variables get consumed by the kernel Makefile to zero-out timestamps
# and uid/gid info that would otherwise leak into the binary.
ENV KBUILD_BUILD_USER=smolvm \
    KBUILD_BUILD_HOST=smolvm-ci \
    KBUILD_BUILD_TIMESTAMP="2026-01-01 00:00:00 UTC" \
    SOURCE_DATE_EPOCH=1767225600

WORKDIR /work
ENTRYPOINT ["/bin/bash"]
