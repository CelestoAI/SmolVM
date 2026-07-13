# Install SmolVM

Install SmolVM, prepare the machine that will run sandboxes, and confirm that it is ready. You need Python 3.10 or newer; supported host setup is Linux and macOS.

## Install the package

```bash
pip install smolvm
```

## Prepare your machine

On macOS, install QEMU first, then let SmolVM check the rest of the setup:

```bash
brew install qemu
smolvm setup
smolvm doctor
```

On Linux, `setup` installs or checks the runtime dependencies and configures what SmolVM needs to run sandboxes. It may ask for administrator permission.

```bash
smolvm setup
smolvm doctor
```

`smolvm doctor` reports problems and their recovery steps. Add `--strict` when a warning should fail an automated check.

## Build-machine setup

Use this only when preparing a reusable machine image on a builder that cannot run virtualization itself:

```bash
smolvm setup --for-bake --runtime-user ubuntu
```

After booting that image on the real runtime machine, run `smolvm doctor` before accepting work.

## What SmolVM selects automatically

When you do not choose a backend, SmolVM uses QEMU on macOS and Firecracker on other hosts. You can inspect a specific choice with `smolvm doctor --backend qemu` or `smolvm doctor --backend firecracker`.

**Implementation notes:** supported setup platforms and packaged setup scripts are defined in [`src/smolvm/host/setup.py`](../src/smolvm/host/setup.py); backend selection is in [`src/smolvm/runtime/backends.py`](../src/smolvm/runtime/backends.py) and is covered by [`tests/test_setup.py`](../tests/test_setup.py) and [`tests/test_backends.py`](../tests/test_backends.py).
