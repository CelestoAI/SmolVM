# Work on `smolvm-core`

`smolvm-core` is the native helper package installed with SmolVM on supported platforms. Most users never import it directly; contributors use it for accelerated host-side capabilities.

## Check your local build

```bash
python -m smolvm_core
```

The command prints a capability report. Rebuild the local extension after changing its Rust source before judging a behavior.

## Where to work

The package, its public Python modules, migration notes, and release versioning live in [`smolvm-core/README.md`](../../smolvm-core/README.md). The top-level package declares it as a workspace dependency in [`pyproject.toml`](../../pyproject.toml), while SmolVM's integration coverage is in [`tests/test_smolvm_core.py`](../../tests/test_smolvm_core.py).

Keep this package's public API separate from its private extension boundary. If a SmolVM guide depends on native behavior, link to the public capability check rather than assuming the native feature exists.
