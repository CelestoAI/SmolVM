# Kernel config

## `smolvm.fragment` — the SmolVM-specific decisions

A Kconfig fragment in Linux's [merge_config](https://www.kernel.org/doc/html/latest/kbuild/kconfig.html#merging-configurations) format. Encodes **only** the symbols where SmolVM differs from `kvm_guest.config`, so diffs stay reviewable.

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
