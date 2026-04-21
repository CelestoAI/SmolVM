# Kernel config

This directory holds the Linux kernel configuration that controls what the SmolVM guest supports at boot — things like shared file mounts, serial console, and which virtual hardware drivers are compiled in. Review changes here carefully: they decide what works, what's bloat, and what's attack surface.

## `smolvm.fragment` — the SmolVM-specific decisions

`smolvm.fragment` is a small kernel-config overlay in Linux's [`merge_config`](https://www.kernel.org/doc/html/latest/kbuild/kconfig.html#merging-configurations) format. It records **only** the symbols where SmolVM differs from upstream's `kvm_guest.config` baseline — so diffs stay reviewable, and we're not hand-maintaining a 5000-line file.

At build time, the flow is:

```
make ARCH=<arch> kvm_guest.config                         # upstream baseline
scripts/kconfig/merge_config.sh .config smolvm.fragment   # our overlays
make ARCH=<arch> olddefconfig                             # resolve new symbols
```

The result is the `.config` that actually drives the kernel build.

## `smolvm-<arch>.config` — full expanded configs (not checked in yet)

Planned for a follow-up PR once the first CI run captures full configs as build artifacts. The intent is to promote them into `config/` and switch `build.sh` to use them verbatim (no more fragment merging at build time). That's the long-term reproducibility story.

## When to bump

Any change to `smolvm.fragment` — even a single `is not set` flip — must bump `kernel/VERSION`, because the produced vmlinux/Image binary is different and the tarball goes out with a new tag.
