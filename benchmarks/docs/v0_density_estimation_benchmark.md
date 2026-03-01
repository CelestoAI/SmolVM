SmolVM Benchmarking Plan 🧪

Repo cloned (workspace/SmolVM-benchmarks). 

Existing benches: command latency, file I/O (ralph JSONL sim), net I/O. 

No density script—custom needed. 

SmolVM: Firecracker/KVM backend, ~572ms boot, low overhead. Firecracker hits 1000s/host in Lambda-scale, but per-instance varies (e.g., OCI BM > VM nested).

1. Density (Max VMs)
Local Test First (your host equiv Ryzen 7):
```from smolvm import SmolVM
vms = []
i = 0
while True:
    try:
        vm = SmolVM(mem_size_mib=128, disk_size_mib=512)  # Minimal
        vm.start()
        vms.append(vm)
        i += 1
        print(f"VM {i} OK")
    except Exception as e:
        print(f"Max: {i}, Fail: {e}")
        break
```
Ramp until OOM/KVM slots (~host vCPUs). 

Metrics: ps -o rss,vsz %cpu per PID, /sys/kernel/debug/kvm slots.


| Instance | vCPU | Mem (GiB) | Est Max VMs |
|----------|------|----------|------------|
| t4g.nano | 2 | 0.5 | ~10-20 |
| t3.micro | 2 | 1 | ~20-50 |
| m5.large | 2 | 8 | ~100-200 |
| c5.xlarge | 4 | 8 | ~150-300 |

Automate: Boto3 launch, SSM run test, CloudWatch mem/CPU.

GCE: Similar (e2-micro/standard-2/4), gcloud compute.


Skepticism Mode Review: SmolVM Benchmarking Plan 🧐

(Embodying Skepticism Mode: Questioning assumptions, probing unverified claims, prioritizing precision/evidence over optimism. "So What?" test: Does this measure real value for AI agent workloads? Sources: memory/project-smolvm.md; repo benches; Firecracker docs.)

1. Core Assumptions Flagged (Unproven/Fragile)
    - Minimal Config Realism: 128MB mem/512MB disk "minimal" VMs—fine for boot tests, but ignores real workloads. AI agents run LLMs/tools: Need 1-4GB+ for pip install torch, API calls, or even curl. Assumption: Toy VMs proxy production? No—measure tiered: tiny (idle), small (Python script), med (LLM inference).

    - Firecracker Equivalence: SmolVM wraps Firecracker. Density = SmolVM overhead + Firecracker base. Plan doesn't isolate: What's raw Firecracker max? (Lambda claims ~4k/node, but EC2 varies 100-1k/instance per blogs/arXiv.)

    - Cloud Nested Virt Penalty: EC2/GCE VMs = nested KVM. Firecracker docs warn 20-50% perf hit vs bare-metal. t3.micro (lite) lacks dedicated KVM accel. Verify baseline on metal/EC2 bare-metal (i3.metal).

    - OOM/KVM as Sole Limiters: Ignores host limits: /proc/sys/kernel/threads-max, netif (TAP FD exhaustion >500 VMs), IRQ saturation, cgroup v2 memsw.

1. Missing Metrics (What You're Not Measuring—Critical Gaps)


| Category | Plan Has | Missing (Why Matters) | Suggested Add |
|----------|----------|----------------------|---------------|
| Density | Max concurrent boot | Sustained uptime/crash rate (e.g., 1hr idle/active); Ramp-up time (boot 100 VMs serially? Parallel?); Teardown speed (leakage?); Per-VM breakdown (firecracker PID RSS/VSZ/CPU; guest mem via free; KVM slots via ls /dev/kvm-vm*; cat /sys/kernel/debug/kvm/max_vms). | htop --filter=firecracker; kvm stats; aggregate psutil script. |
| Performance | Boot/cmd/file/net I/O | CPU-bound (sysbench/prime95 guest); Multi-VM contention (contention @50%/90% density); Tail latency (p99 cmd exec @100 VMs); Power/$$ (cloud: CPU credits/cost/hour at density); Net saturation (iperf3 multi-stream @ density). | fio (rand/seq RW); stress-ng CPU; perf top (host KVM irq). |
| I/O Depth | File (JSONL sim) | Block device (virtio vs NVMe passthru); Multi-VM I/O storm (all VMs randwrite sim); Host cache effects (fio --direct=1); Guest fs (ext4 vs tmpfs). | fio --name=randrw --bs=4k --iodepth=32; blktrace host. |
| Comparisons | Vs Firecracker/KVM | Raw baselines (no SmolVM wrapper); Containers (Docker/podman for context); gVisor (kernel sim). | Fork Firecracker repo; docker stats. |
| Stability/Safety | N/A | Attack surface (escape tests?); Resource leaks (post-kill mem?); Host impact (temp rise, net flood). | falco/auditd guest-host; valgrind firecracker? |


1. Risks/Edge Cases Overlooked
    - Fork Drift: tensor-ninja vs CelestoAI—bench both? Latest commits?
    - Host Tuning: No /etc/sysctl (e.g., net.core.somaxconn, vm.swappiness=0)? Firecracker needs modprobe vhost_vsock.
    - Reproducibility: Single-run? No stats (mean/std/p95)? No CI (GitHub Actions matrix)?
    - Scale Reality: 300 VMs/c5.xlarge optimistic—Lambda tunes kernel specially. EC2 spot kills mid-test.
    - "So What?" Fail: Max density cool, but throughput/$$ per VM matters more for agents (e.g., VMs/hour * success rate).

1. Revised Plan Skeleton (Precision Fixes)
Phases:
    1. Local Baseline: Tune host → Run repo benches → Density ramp (3 tiers) → Stats table.
    2. Raw Firecracker: Build/run equivalent VMs.
    3. Cloud: Bare-metal → Nested → Automate (terraform + boto3/gcloud).

- Output: CSV (inst|density|max_mem|boot_p95|io_mb/s|cost/hr) + graphs.
- Cost Guardrail: t3.micro spot ~$0.002/hr; cap 10 instances.

- Verdict: Plan 6/10—good start (local/EC2 targets), but toy-focused, no contention/realism, weak baselines. Fix gaps → actionable.