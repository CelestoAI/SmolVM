# Running Windows 11 as a Guest under QEMU/KVM

Boot a Windows 11 desktop inside SmolVM that behaves like a real Windows
PC. Useful when you need to run Windows-only software, test something
against Windows, or hand an agent a Windows desktop without touching your
own machine.

This page explains every QEMU flag the Windows path needs and why,
distilled from QEMU upstream, libvirt, Red Hat, Proxmox, Arch, Microsoft,
TianoCore (EDK II), and the virtio-win project. Sources are linked
inline; full citations live in the footnotes at the bottom.

## The picture in one paragraph

A modern Windows 11 guest under QEMU is not just "the Linux command line with
a Windows ISO." Microsoft requires **UEFI + Secure Boot + TPM 2.0**, which
forces **q35 + split OVMF (.secboot build) + swtpm** as a baseline. On top of
that, Windows performance hinges on two paravirt knobs the Linux guests don't
need: **Hyper-V enlightenments** (so Windows takes its fast in-guest paths)
and a **virtio-scsi** disk + the **virtio-win** driver ISO loaded mid-install
so Windows can see the disk at all. Everything else (CPU pinning, NUMA,
hugepages, tap+bridge networking, vhost-net) is opt-in tuning that doesn't
apply to a short-lived sandbox.

## The reference command line

A safe, production-quality starting point for a Windows 11 guest on a modern
Linux KVM host (Debian/Ubuntu paths shown). Every flag is sourced in the
per-area sections below.

```bash
# Sidecar: per-VM software TPM
swtpm socket \
  --tpmstate dir=/var/lib/smolvm/win11/tpm \
  --ctrl type=unixio,path=/var/lib/smolvm/win11/tpm/swtpm-sock \
  --tpm2 --daemon

# The VM itself
qemu-system-x86_64 \
  -name win11 \
  -machine type=pc-q35-9.1,accel=kvm,kernel-irqchip=on,smm=on,vmport=off \
  -cpu host,hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,hv_vpindex,hv_runtime,\
hv_time,hv_synic,hv_stimer,hv_frequencies,hv_tlbflush,hv_ipi,hv_reset,\
+kvm_pv_eoi,+kvm_pv_unhalt \
  -smp 4,sockets=1,cores=4,threads=1 \
  -object memory-backend-memfd,id=mem,size=8G,share=on \
  -machine memory-backend=mem \
  \
  -global driver=cfi.pflash01,property=secure,value=on \
  -global ICH9-LPC.disable_s3=1 \
  -drive if=pflash,format=raw,unit=0,readonly=on,\
file=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd \
  -drive if=pflash,format=raw,unit=1,\
file=/var/lib/smolvm/win11/OVMF_VARS.fd \
  \
  -chardev socket,id=chrtpm,path=/var/lib/smolvm/win11/tpm/swtpm-sock \
  -tpmdev emulator,id=tpm0,chardev=chrtpm \
  -device tpm-crb,tpmdev=tpm0 \
  \
  -object iothread,id=iothr0 \
  -device virtio-scsi-pci,id=scsi0,iothread=iothr0,num_queues=4 \
  -blockdev '{"driver":"qcow2","node-name":"win11-fmt",\
"file":{"driver":"file","filename":"/var/lib/smolvm/win11/disk.qcow2",\
"aio":"io_uring","cache":{"direct":true,"no-flush":false}},\
"discard":"unmap","detect-zeroes":"unmap",\
"cache":{"direct":true,"no-flush":false}}' \
  -device scsi-hd,bus=scsi0.0,drive=win11-fmt,id=win11-disk,bootindex=2 \
  \
  -drive file=/path/to/Win11.iso,media=cdrom,if=none,id=installmedia \
  -device ide-cd,bus=ide.0,drive=installmedia,bootindex=1 \
  -drive file=/path/to/virtio-win.iso,media=cdrom,if=none,id=drivermedia \
  -device ide-cd,bus=ide.1,drive=drivermedia \
  \
  -netdev user,id=net0,hostfwd=tcp::2222-:22 \
  -device virtio-net-pci,netdev=net0,mac=52:54:00:5d:00:01 \
  \
  -device virtio-vga-gl -display sdl,gl=on \
  -device qemu-xhci,id=xhci \
  -device usb-tablet,bus=xhci.0
```

Two non-obvious flag choices in there, both learned from real-world testing
([end-to-end walkthrough below](#end-to-end-bring-up-walkthrough) has the
gory details):

- **Install + driver CDs are on `ide-cd` (AHCI/SATA), not `scsi-cd`.** Windows
  has inbox AHCI drivers but no inbox virtio-scsi driver, so putting the
  install media on virtio-scsi works at boot (OVMF reads it) but breaks the
  moment the user loads `vioscsi` mid-install — Windows switches its SCSI
  driver and loses access to the install media on the same bus, producing
  the classic *"A media driver your computer needs is missing"* error. Each
  AHCI port holds exactly one drive, so the two CDs go on `ide.0` and
  `ide.1`.
- **`usb-tablet` needs an explicit `qemu-xhci`.** `pc-q35-*` ships no
  default USB host controller (unlike `pc-i440fx-*` which provides PIIX3
  USB on `usb-bus.0`), so plugging the tablet directly fails to find a bus.

One-time per-VM prep:

```bash
sudo install -d -m 0700 /var/lib/smolvm/win11/tpm
sudo cp /usr/share/OVMF/OVMF_VARS_4M.ms.fd /var/lib/smolvm/win11/OVMF_VARS.fd
qemu-img create -f qcow2 -o preallocation=falloc,cluster_size=64K \
  /var/lib/smolvm/win11/disk.qcow2 64G
```

## Per-area synthesis

### 1. Machine type: q35, not i440fx

q35 emulates the 2007 Q35 + ICH9 chipset with a **PCIe root complex, AHCI
SATA, EHCI/UHCI USB 2.0** — i440fx is the 1996 PIIX3 box with legacy PCI and
IDE. Red Hat already deprecated i440fx in RHEL 10[^rh-q35] and Windows 11's
Secure Boot story requires the SMM-protected pflash that the documented
recipe pairs with q35.[^tianocore-smm] Pin a versioned machine type
(`pc-q35-9.1`) so Windows' hardware fingerprint stays stable across QEMU
upgrades (affects activation).

Key sub-options:

- `accel=kvm` — in-kernel hypervisor.
- `kernel-irqchip=on` — fastest interrupt path.
- `smm=on` — required for Secure Boot variable protection.[^edk2-readme]
- `vmport=off` — drop the VMware backdoor port; Windows has no driver for
  it.[^qemu-invocation]

### 2. Hyper-V enlightenments: not optional

When QEMU advertises the Hyper-V CPUID leaves, Windows switches on its
**paravirt fast paths** — synthetic timers, paravirt spinlocks, paravirt IPIs
and TLB shoot-downs, MSR-based clocksource — that replace expensive
trap-and-emulate operations.[^qemu-hyperv] QEMU upstream's own guidance:
*"enable all currently implemented Hyper-V enlightenments with the following
exceptions: hv-syndbg, hv-passthrough, hv-enforce-cpuid should not be enabled
in production."*[^qemu-hyperv] The flag set above
(`hv_relaxed,hv_vapic,hv_spinlocks=0x1fff,hv_vpindex,hv_runtime,hv_time,
hv_synic,hv_stimer,hv_frequencies,hv_tlbflush,hv_ipi,hv_reset`) is the
convergent baseline across QEMU upstream, libvirt, Proxmox, and Red
Hat.[^proxmox-hyperv][^rh-windows]

**Don't** set `kvm=off` or override `hv_vendor_id` by default. The historical
Nvidia "Error 43" workaround has been unnecessary since Nvidia driver R465
(March 2021).[^nvidia-r465]

### 3. CPU topology: shape it, don't let QEMU default

`-smp 4` without shape can hand Windows 10/11 Home **a single usable vCPU** —
Home caps at 1 socket, Pro at 2, and QEMU pre-6.2 defaults to
sockets-first.[^win-sockets] Always pass `sockets=1,cores=N,threads=1` for
guests up to ~64 vCPUs.

### 4. Firmware: split OVMF + Microsoft-keys VARS + SMM

Windows 11 mandates UEFI + Secure Boot + TPM 2.0.[^win11-spec] That maps to a
specific firmware shape:

- **`OVMF_CODE_4M.secboot.fd`** (read-only, shared) — the `.secboot` build is
  compiled with `SECURE_BOOT_ENABLE` *and* `SMM_REQUIRE`; only this build
  enforces Secure Boot.[^edk2-readme]
- **Per-VM copy of `OVMF_VARS_4M.ms.fd`** (writable) — the `.ms` template has
  the Microsoft Windows Production PCA 2011 and Microsoft UEFI CA 2011 keys
  pre-enrolled in `db`.[^debian-sb]
- **`-machine smm=on` + `-global driver=cfi.pflash01,property=secure,value=on`
  + `-global ICH9-LPC.disable_s3=1`** — only with all three does SMM actually
  protect the variable store from in-guest tampering.[^debian-sb][^edk2-readme]

**Use the 4M-suffix images on Debian/Ubuntu.** The Arch wiki specifically
warns that the 2M variants cause Windows 11 setup to fail to detect TPM
2.0.[^arch-qemu]

Distro paths to discover:

| Distro | CODE file | VARS file (with Microsoft keys enrolled) |
|---|---|---|
| Debian / Ubuntu | `/usr/share/OVMF/OVMF_CODE_4M.secboot.fd` | `/usr/share/OVMF/OVMF_VARS_4M.ms.fd` |
| Fedora / RHEL | `/usr/share/edk2/ovmf/OVMF_CODE.secboot.fd` | `/usr/share/edk2/ovmf/OVMF_VARS.secboot.fd` |
| Arch | `/usr/share/edk2/x64/OVMF_CODE.secboot.4m.fd` | `/usr/share/edk2/x64/OVMF_VARS.4m.fd` |
| macOS Homebrew | `/opt/homebrew/share/qemu/edk2-x86_64-code.fd` | `/opt/homebrew/share/qemu/edk2-i386-vars.fd` |

### 5. vTPM via swtpm

swtpm is a separate userspace daemon (`brew install swtpm` or distro
packages) that emulates a TPM 2.0 over a Unix socket; QEMU connects to it as
a client via `-tpmdev emulator,id=tpm0,chardev=chrtpm`.[^qemu-tpm] Use
**`tpm-crb`** for Windows 11 — it's the modern interface that matches what
physical Windows 11 hardware uses; `tpm-tis` is the older fallback if a
specific build doesn't detect the TPM.[^qemu-tpm]

**macOS caveat: `swtpm` builds via Homebrew, but the rest of the stack falls
apart.** Windows x86_64 on Apple Silicon QEMU is TCG-only (no HVF for x86 on
ARM) — correctness-OK, unusably slow.[^homebrew-swtpm] Windows-on-ARM under
HVF + ARM swtpm + `virt` machine is the only practical Mac path, with QEMU
explicitly warning of TPM PPI errors.[^qemu-tpm] **Treat Windows guests as
Linux-host-only for v1.**

### 6. Disks: virtio-scsi with the works

The convergent recipe from Red Hat, Proxmox, and QEMU upstream is
**virtio-scsi-single + IOThread + qcow2 + cache=none + discard=unmap +
detect-zeroes=unmap + aio=io_uring**.[^proxmox-perf][^qemu-virtio-blk-scsi][^anteru-trim]

- **virtio-scsi over virtio-blk** because it supports many LUNs per PCI slot,
  real SCSI UNMAP for qcow2 shrink, persistent reservations, and
  CD-ROMs.[^qemu-virtio-blk-scsi] Windows boots off SCSI fine *after*
  `vioscsi` is loaded from the virtio-win ISO during install — without it,
  setup sees no disk.[^anteru-trim]
- **`cache=none`** (= `cache.direct=true, cache.no-flush=false`) avoids
  double-caching while Windows still flushes correctly.[^suse-cache] **Never
  `cache=unsafe`** outside throwaway OS installs.
- **`aio=io_uring`** on kernels ≥ 5.13 / QEMU ≥ 6.0; otherwise fall back to
  `aio=threads` (works on any backing storage including
  NFS).[^blockbridge-aio] `aio=native` only with `O_DIRECT` + raw block
  storage.
- **`discard=unmap` + `detect-zeroes=unmap`** plumb Windows' TRIM commands
  (issued weekly by Optimize Drives once the disk is flagged thin-provisioned)
  all the way down to qcow2 cluster deallocation — the qcow2 file actually
  shrinks.[^anteru-trim] Known wart: virtio-win issue #666 (Win10 Optimize
  against vioscsi can take 10–15 min vs ~5s bare-metal, but still
  works).[^virtiowin-666]
- **`num_queues=N`** sized to vCPU count; the Windows `vioscsi` driver has
  been multiqueue-aware since 2016.[^rh-vioscsi-mq]
- **`-object iothread`** to move block I/O off the main QEMU event loop —
  ~15% latency improvement at QD=1.[^proxmox-perf]
- **`qemu-img create -o preallocation=falloc,cluster_size=64K`** to avoid
  first-write allocation stalls without paying the time cost of
  `preallocation=full`.[^qemu-img]

### 7. Networking: keep SLIRP + hostfwd as the default

SLIRP (userspace NAT) is slow and ICMP-broken, but requires zero root and
zero host config — which matches SmolVM's existing posture for Linux guests.
For Windows + the NetKVM virtio-net driver, the backend is transparent:
NetKVM sees a virtio-net PCI device and doesn't care whether SLIRP,
tap+bridge, macvtap, or vhost is behind it.[^netkvm]

```bash
-netdev user,id=net0,hostfwd=tcp::2222-:22
-device virtio-net-pci,netdev=net0,mac=52:54:00:...
```

Host SSHes the guest with `ssh -p 2222 Administrator@127.0.0.1`. Use a stable
locally-administered MAC (`52:54:00:` OUI) so Windows doesn't keep prompting
"is this a public network?" on each boot.[^qemu-net]

**Opt-in upgrades** to document but not ship as default:

- **`qemu-bridge-helper` + libvirt's `virbr0`** when the user already has
  libvirt installed — `-netdev bridge,br=virbr0`, no root needed at run time
  after a one-line `/etc/qemu/bridge.conf`.[^mike42-helper]
- **`tap + vhost=on`** for ~8× throughput; needs persistent root-created
  tap.[^kvm-vhost]
- **Skip macvtap** — the host-can't-talk-to-guest gotcha will burn
  users.[^cmu-macvtap]

### 8. CPU pinning, NUMA, hugepages: skip for sandboxes

For a short-lived sandbox VM, **none of this is worth the complexity.**
Honest numbers from Red Hat's own benchmark: static 1 GiB hugepages beat THP
by **1–2%** on realistic workloads.[^rh-thp] CPU pinning saves single-digit %
only under host contention and *removes* the scheduler's ability to
load-balance.[^rh-cpupin] NUMA is irrelevant on the single-socket consumer
hosts SmolVM targets.[^proxmox-numa]

**Default to:** topology shaped as `sockets=1,cores=N,threads=1`,
`memory-backend-memfd` (lets THP work), one IOThread per disk, no pinning,
no static hugepages, no explicit NUMA. Expose pinning/hugepages as
`--pin-cpus` / `--hugepages` opt-in flags for users running long-lived
Windows VMs.

## End-to-end bring-up walkthrough

What follows is the step-by-step the user actually walks through to take a
blank `disk.qcow2` to a Windows desktop with SSH reachable on
`localhost:2222`. Each step calls out gotchas we hit; the QEMU configuration
choices above were made specifically so this walkthrough works at all.

### 0. Pre-flight — running on a headless host

If the QEMU host has no graphical session (server, SSH-only access), the
`-display vnc=127.0.0.1:0` variant of the reference command line is the
right pick — and the user connects from their laptop via an SSH tunnel:

```bash
# On the laptop, forward both the VNC display and the guest-SSH port:
ssh -L 5900:127.0.0.1:5900 -L 2222:127.0.0.1:2222 <user>@<qemu-host>

# Then on the laptop:
vncviewer 127.0.0.1:5900
ssh -p 2222 <guest-user>@127.0.0.1   # after sshd is up in the guest
```

Keep that first tunnel session open while you work; closing it tears down
both forwards.

### 1. OVMF boot menu — pick the Windows ISO

At the splash screen, hit **Esc** for the boot menu. You'll see entries
named `UEFI QEMU CD-ROM` and `UEFI QEMU CD-ROM 2`. The first one (no `2`)
is the Windows installer — that's what `bootindex=1` on the install media
gives us. The second is the virtio-win disc, which is not UEFI-bootable
and would just fall through.

### 2. Load the vioscsi driver — *required* to see the disk

At **"Where do you want to install Windows?"** (new 24H2 wording is **"Install
drivers to display drives"**) the disk list is empty because the boot disk
is on virtio-scsi and Windows has no inbox driver for it. Click **Load
Driver → Browse**, then navigate to the **virtio-win CD** (labeled something
like `virtio-win-0.1.285`) and pick:

```cmd
vioscsi → w11 → amd64
```

Click **OK**, select **Red Hat VirtIO SCSI controller**, then **Next**. The
64 GiB disk appears. Optionally load **NetKVM** and **Balloon** from the
same dialog now to save a step during OOBE.

### 3. OOBE — bypass the internet wall

Windows 11 OOBE refuses to continue past the network screen until it has
internet. Without NetKVM loaded yet, you're stuck. Two ways out:

**Right answer — install NetKVM inline.** `Shift+F10` opens a Command
Prompt. Find the virtio-win drive letter, then load the driver:

```cmd
wmic logicaldisk get caption,volumename
pnputil /add-driver E:\NetKVM\w11\amd64\netkvm.inf /install
```

(Replace `E:` with whatever drive letter the virtio-win CD got.) Close the
window; OOBE detects the NIC within seconds, SLIRP hands out a DHCP lease
on `10.0.2.15`, and you can continue.

**Shortcut — skip the internet requirement.** Same `Shift+F10`, then:

```cmd
start ms-cxh:localonly
```

This jumps straight to local-account creation. On older Windows 11 builds
the magic spell was `OOBE\BYPASSNRO` instead (deprecated in 24H2).

### 4. OOBE — local account, not Microsoft account

At the sign-in screen, same `Shift+F10 → start ms-cxh:localonly` trick
opens a local-account creation dialog. Use a known username/password (you
need both for SSH later). For a sandbox, never link to a real Microsoft
account.

### 5. Post-install — OpenSSH server, the careful way

The supported path is the Windows Update capability:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
```

**Critical gotcha:** this command returns a success-shaped object
(`Online: True`, `RestartNeeded: False`) **even when the underlying download
from Microsoft's CDN is blocked, queued, or otherwise failing**. Verify
explicitly:

```powershell
Get-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 |
  Format-List Name,State
```

`State : NotPresent` means it didn't install. In practice the install often
completes only after a Windows-Update-triggered reboot has finished a
separate background update — there's no clean signal, you just retry. Once
`State : Installed`:

```powershell
Start-Service sshd
Set-Service sshd -StartupType Automatic
New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server (sshd)' `
  -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22
```

The capability install usually adds the firewall rule itself; the
`New-NetFirewallRule` call is idempotent.

**WU-independent fallback.** If the capability install keeps failing,
download `OpenSSH-Win64.zip` from `github.com/PowerShell/Win32-OpenSSH`
releases and run `install-sshd.ps1`. Deterministic, no Windows Update
dependency, ~20 seconds. For SmolVM's eventual `windows` preset this is the
better default — fewer externalities.

### 6. Post-install — full virtio driver set

Open the virtio-win CD in Explorer and run **`virtio-win-guest-tools.exe`**.
This installs the remaining drivers (Balloon, vioserial, viorng, vioinput,
viogpu) plus the QEMU Guest Agent in one shot. Reboot when prompted.

### 7. Snapshot the baseline

In the guest: **Start → Power → Shut down** (a *clean* shutdown — don't
power-off the QEMU process). Once Windows is off and `start-vm.sh` has
exited:

```bash
cp ~/win11-vm/disk.qcow2 ~/win11-vm/disk-baseline.qcow2
```

That gives you a snapshot of the "freshly installed, all drivers in,
SSH-able" state. Any future experiment that breaks Windows rolls back in
seconds:

```bash
cp ~/win11-vm/disk-baseline.qcow2 ~/win11-vm/disk.qcow2
```

For SmolVM's eventual model this is the *base image* — every sandbox is
either a clone of (or a qcow2 overlay on top of) this baseline.

### 8. Automation — `autounattend.xml`

The entire walkthrough above is interactive. Windows Setup supports a
standard answer file (`autounattend.xml`) that drives the installer
unattended end-to-end: region/keyboard, viostor driver pre-load, partition
table, local-account creation, OOBE skip, plus `FirstLogonCommands` to
install OpenSSH and virtio-win-guest-tools. Microsoft documents the schema
at [learn.microsoft.com/en-us/windows-hardware/customize/desktop/unattend/](https://learn.microsoft.com/en-us/windows-hardware/customize/desktop/unattend/).

The mechanics: build a tiny FAT-formatted ISO containing just
`autounattend.xml` at the root (`genisoimage -V autounattend -o
autounattend.iso autounattend.xml`), attach it as a third CD-ROM, boot the
Windows installer normally. Windows Setup auto-detects the answer file on
any attached removable media — no flag to QEMU required.

This is **not** yet implemented for this repo. Punch-list item for the
SmolVM `windows` preset: ship a parameterized `autounattend.xml.j2` so a
fresh Windows base image can be built from `Win11.iso + virtio-win.iso`
with no human input.

## Implications for SmolVM (phase 1 scope)

Refining the "boot only" phase 1 with what we now know:

1. **Data-model change** — add `guest_os: Literal["linux","windows"]` to
   `ImageSource`/`LocalImage`, threading through to the QEMU command builder.
2. **OVMF discovery on x86_64** — `host/_accel.py` currently checks aarch64
   OVMF paths only (`vm.py:137-153`). Add an x86_64 search covering the four
   distro paths above. Raise a clear error when missing — don't auto-download.
3. **Per-VM VARS file** — copy `OVMF_VARS_4M.ms.fd` into the VM's state
   directory on first boot.
4. **swtpm sidecar** — new lifecycle component. Spawn
   `swtpm socket --tpm2 --daemon` before QEMU, tear down on VM stop.
   Linux-host-only for v1 (skip macOS).
5. **Switch the disk default to virtio-scsi for Windows** — current QEMU
   backend uses virtio-blk; Windows wants virtio-scsi with iothread +
   `discard=unmap` + `detect-zeroes=unmap` + `aio=io_uring`.
6. **virtio-win ISO handling** — fetch + cache it like other published
   images, attach as a second CD-ROM during install. The user supplies the
   Windows ISO.
7. **CPU/machine flag preset** — bundle the q35 + hyperv enlightenments +
   SMM string as a `WindowsGuestProfile` rather than scattering them across
   the QEMU command builder.
8. **Networking stays on `user,hostfwd=`** — same as Linux guests today,
   just port-forward 22 to the Windows OpenSSH server.
9. **No 9p workspace mounts in v1** — Windows 9p support is experimental;
   document `--no-workspace` as the default for Windows guests.
10. **Drop CPU pinning / NUMA / hugepages from scope entirely** for v1.
    Maybe `--memfd-memory-backend` as a quiet default change so THP works.

This keeps phase 1 to: data-model flag + x86_64 OVMF discovery + per-VM VARS
handling + swtpm spawn/teardown + disk-profile switch + virtio-win attach.

---

[^qemu-hyperv]: QEMU — Hyper-V Enlightenments, <https://www.qemu.org/docs/master/system/i386/hyperv.html>
[^rh-windows]: Red Hat RHEL 10 — Optimizing Windows VMs, <https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/configuring_and_managing_windows_virtual_machines/optimizing-windows-virtual-machines>
[^proxmox-hyperv]: Proxmox forum — Windows CPU flags, <https://forum.proxmox.com/threads/vm-cpu-flags-usage-which-ones-should-be-enabled-for-max-performance-and-how.156457/>
[^nvidia-r465]: Nvidia KB 5173 — GeForce GPU passthrough for Windows VMs, <https://nvidia.custhelp.com/app/answers/detail/a_id/5173/>
[^rh-q35]: Red Hat RHEL 10 — Converting VMs to the Q35 machine type, <https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/configuring_and_managing_windows_virtual_machines/converting-virtual-machines-to-q35>
[^tianocore-smm]: TianoCore wiki — Testing SMM with QEMU/KVM/libvirt, <https://github.com/tianocore/tianocore.github.io/wiki/Testing-SMM-with-QEMU,-KVM-and-libvirt>
[^qemu-invocation]: QEMU manual — Invocation (`-machine` sub-options), <https://www.qemu.org/docs/master/system/invocation.html>
[^win11-spec]: Microsoft — Windows 11 Specifications, <https://www.microsoft.com/en-us/windows/windows-11-specifications>
[^edk2-readme]: TianoCore — OvmfPkg/README, <https://github.com/tianocore/edk2/blob/master/OvmfPkg/README>
[^debian-sb]: Debian Wiki — SecureBoot/VirtualMachine, <https://wiki.debian.org/SecureBoot/VirtualMachine>
[^arch-qemu]: Arch Wiki — QEMU, <https://wiki.archlinux.org/title/QEMU>
[^qemu-tpm]: QEMU TPM Device specs, <https://qemu-project.gitlab.io/qemu/specs/tpm.html>
[^homebrew-swtpm]: Homebrew formula — swtpm, <https://formulae.brew.sh/formula/swtpm>
[^win-sockets]: Codeinsecurity — Windows 10 socket/core limits teardown, <https://codeinsecurity.wordpress.com/2022/04/07/cpu-socket-and-core-count-limits-in-windows-10-and-how-to-remove-them/>
[^qemu-virtio-blk-scsi]: QEMU blog — Configuring virtio-blk and virtio-scsi, <https://www.qemu.org/2021/01/19/virtio-blk-scsi-configuration/>
[^proxmox-perf]: Proxmox wiki — Qemu/KVM Virtual Machines, <https://pve.proxmox.com/wiki/Qemu/KVM_Virtual_Machines>
[^suse-cache]: SUSE — Disk Cache Modes, <https://documentation.suse.com/sles/12-SP5/html/SLES-all/cha-cachemodes.html>
[^blockbridge-aio]: Blockbridge — Optimizing Proxmox iothreads, aio, io_uring, <https://kb.blockbridge.com/technote/proxmox-aio-vs-iouring/>
[^anteru-trim]: Anteru — QEMU, KVM and trim, <https://anteru.net/blog/2020/qemu-kvm-and-trim/>
[^virtiowin-666]: virtio-win issue #666 — Win10 Optimize with vioscsi, <https://github.com/virtio-win/kvm-guest-drivers-windows/issues/666>
[^rh-vioscsi-mq]: Red Hat Bugzilla 1210166 — vioscsi multiqueue, <https://bugzilla.redhat.com/show_bug.cgi?id=1210166>
[^qemu-img]: QEMU qemu-img docs, <https://www.qemu.org/docs/master/tools/qemu-img.html>
[^netkvm]: virtio-win/kvm-guest-drivers-windows — NetKVM, <https://deepwiki.com/virtio-win/kvm-guest-drivers-windows/2-network-drivers-(netkvm)>
[^qemu-net]: QEMU/Networking — Wikibooks, <https://en.wikibooks.org/wiki/QEMU/Networking>
[^mike42-helper]: mike42.me — qemu-bridge-helper on Debian 10, <https://mike42.me/blog/2019-08-how-to-use-the-qemu-bridge-helper-on-debian-10>
[^kvm-vhost]: linux-kvm.org — UsingVhost, <https://www.linux-kvm.org/page/UsingVhost>
[^cmu-macvtap]: CMU — KVM Macvtap vs bridging, <https://www.math.cmu.edu/~gautam/sj/blog/20140303-kvm-macvtap.html>
[^rh-thp]: Red Hat Developers — THP vs 1 GiB hugepage benchmark, <https://developers.redhat.com/blog/2021/04/27/benchmarking-transparent-versus-1gib-static-huge-page-performance-in-linux-virtual-machines>
[^rh-cpupin]: Red Hat OpenStack — emulatorpin in NFV, <https://docs.redhat.com/en/documentation/red_hat_openstack_platform/10/html/ovs-dpdk_end_to_end_troubleshooting_guide/using_virsh_emulatorpin_in_virtual_environments_with_nfv>
[^proxmox-numa]: Proxmox NUMA wiki, <https://pve.proxmox.com/wiki/NUMA>
