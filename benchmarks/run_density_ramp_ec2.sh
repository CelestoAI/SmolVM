#!/usr/bin/env bash
# run_density_ramp_ec2.sh
#
# Launches an EC2 instance, deploys and runs the density_ramp benchmark,
# collects results, then terminates the instance.
#
# Prerequisites:
#   - AWS CLI configured (aws configure or IAM role)
#   - An existing EC2 key pair (set KEY_NAME below or via env)
#   - The key's .pem file accessible at KEY_PATH
#
# Usage:
#   ./run_density_ramp_ec2.sh [--tier tiny|small|med] [--max-attempts N] \
#                              [--sustain-sec N] [--parallel N] [--shared-disk] \
#                              [--output FILE]
#
# Environment variable overrides:
#   KEY_NAME        EC2 key pair name (required)
#   KEY_PATH        Path to .pem file (default: ~/.ssh/${KEY_NAME}.pem)
#   INSTANCE_TYPE   EC2 instance type (default: c5.metal)
#   AMI_ID          AMI to use (default: latest Amazon Linux 2023 in us-east-1)
#   REGION          AWS region (default: us-east-1)
#   SECURITY_GROUP  Security group ID (optional; creates a temporary one if unset)
#   SUBNET_ID       Subnet ID (optional; uses default VPC subnet if unset)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (override via environment variables)
# ---------------------------------------------------------------------------
REGION="${REGION:-us-east-2}"
INSTANCE_TYPE="${INSTANCE_TYPE:-c5d.metal}"
KEY_NAME="${KEY_NAME:-smolvm-benchmark}"
KEY_PATH="${KEY_PATH:-}"
SECURITY_GROUP="${SECURITY_GROUP:-}"
SUBNET_ID="${SUBNET_ID:-}"
AMI_ID="${AMI_ID:-}"

# Benchmark args (overridden by CLI flags below)
TIER="tiny"
MAX_ATTEMPTS="500"
SUSTAIN_SEC="60"
PARALLEL="1"
SHARED_DISK=""
OUTPUT_FILE="density_$(date +%Y%m%d_%H%M%S).json"

# ---------------------------------------------------------------------------
# Parse CLI flags
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tier)          TIER="$2";          shift 2 ;;
    --max-attempts)  MAX_ATTEMPTS="$2";  shift 2 ;;
    --sustain-sec)   SUSTAIN_SEC="$2";   shift 2 ;;
    --parallel)      PARALLEL="$2";      shift 2 ;;
    --shared-disk)   SHARED_DISK="--shared-disk"; shift ;;
    --output)        OUTPUT_FILE="$2";   shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Validate prerequisites
# ---------------------------------------------------------------------------
if ! command -v aws &>/dev/null; then
  echo "ERROR: AWS CLI not found. Install it: https://docs.aws.amazon.com/cli/latest/userguide/install-cliv2.html"
  exit 1
fi

if [[ -z "$KEY_NAME" ]]; then
  echo "ERROR: KEY_NAME is not set. Export it or edit this script."
  echo "  export KEY_NAME=my-key-pair"
  exit 1
fi

KEY_PATH="${KEY_PATH:-$HOME/.ssh/${KEY_NAME}.pem}"
if [[ ! -f "$KEY_PATH" ]]; then
  echo "ERROR: Key file not found at $KEY_PATH"
  echo "  Set KEY_PATH to the correct location of your .pem file."
  exit 1
fi
chmod 600 "$KEY_PATH"

# ---------------------------------------------------------------------------
# Resolve AMI (latest Amazon Linux 2023 x86_64)
# ---------------------------------------------------------------------------
if [[ -z "$AMI_ID" ]]; then
  echo "Resolving latest Amazon Linux 2023 AMI in ${REGION}..."
  AMI_ID=$(aws ec2 describe-images \
    --region "$REGION" \
    --owners amazon \
    --filters \
      "Name=name,Values=al2023-ami-2023*-x86_64" \
      "Name=state,Values=available" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" \
    --output text)
  echo "Using AMI: $AMI_ID"
fi

# ---------------------------------------------------------------------------
# Optionally create a temporary security group
# ---------------------------------------------------------------------------
CREATED_SG=""
if [[ -z "$SECURITY_GROUP" ]]; then
  echo "Creating temporary security group..."
  SG_NAME="smolvm-bench-$(date +%s)"

  # Get default VPC
  DEFAULT_VPC=$(aws ec2 describe-vpcs \
    --region "$REGION" \
    --filters "Name=isDefault,Values=true" \
    --query "Vpcs[0].VpcId" \
    --output text)

  SECURITY_GROUP=$(aws ec2 create-security-group \
    --region "$REGION" \
    --group-name "$SG_NAME" \
    --description "Temporary SG for SmolVM density benchmark" \
    --vpc-id "$DEFAULT_VPC" \
    --query "GroupId" \
    --output text)

  # Allow SSH only from the caller's public IP
  CALLER_IP=$(curl -sf https://checkip.amazonaws.com 2>/dev/null \
    || curl -sf https://ifconfig.me 2>/dev/null \
    || echo "")
  if [[ -n "$CALLER_IP" ]]; then
    SSH_CIDR="${CALLER_IP}/32"
  else
    echo "WARNING: could not determine public IP; opening SSH to 0.0.0.0/0"
    SSH_CIDR="0.0.0.0/0"
  fi
  aws ec2 authorize-security-group-ingress \
    --region "$REGION" \
    --group-id "$SECURITY_GROUP" \
    --protocol tcp \
    --port 22 \
    --cidr "$SSH_CIDR" \
    >/dev/null

  CREATED_SG="$SECURITY_GROUP"
  echo "Created security group: $SECURITY_GROUP"
fi

# ---------------------------------------------------------------------------
# Launch EC2 instance
# ---------------------------------------------------------------------------
INSTANCE_ID=""

cleanup() {
  echo ""
  echo "--- Cleanup ---"

  if [[ -n "$INSTANCE_ID" ]]; then
    echo "Terminating instance $INSTANCE_ID..."
    aws ec2 terminate-instances \
      --region "$REGION" \
      --instance-ids "$INSTANCE_ID" \
      >/dev/null
    echo "Termination requested."
  fi

  if [[ -n "$CREATED_SG" ]]; then
    echo "Waiting for instance to terminate before deleting security group..."
    aws ec2 wait instance-terminated \
      --region "$REGION" \
      --instance-ids "$INSTANCE_ID" 2>/dev/null || true
    echo "Deleting security group $CREATED_SG..."
    aws ec2 delete-security-group \
      --region "$REGION" \
      --group-id "$CREATED_SG" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo ""
echo "Launching $INSTANCE_TYPE instance..."

LAUNCH_ARGS=(
  --region "$REGION"
  --image-id "$AMI_ID"
  --instance-type "$INSTANCE_TYPE"
  --key-name "$KEY_NAME"
  --security-group-ids "$SECURITY_GROUP"
  --count 1
  --instance-initiated-shutdown-behavior terminate
  --query "Instances[0].InstanceId"
  --output text
)
if [[ -n "$SUBNET_ID" ]]; then
  LAUNCH_ARGS+=(--subnet-id "$SUBNET_ID")
fi

INSTANCE_ID=$(aws ec2 run-instances "${LAUNCH_ARGS[@]}")
echo "Instance ID: $INSTANCE_ID"

# ---------------------------------------------------------------------------
# Wait for instance to be running and pass status checks
# ---------------------------------------------------------------------------
echo "Waiting for instance to reach 'running' state..."
aws ec2 wait instance-running \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID"

echo "Waiting for system status checks to pass..."
aws ec2 wait instance-status-ok \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID"

PUBLIC_IP=$(aws ec2 describe-instances \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --query "Reservations[0].Instances[0].PublicIpAddress" \
  --output text)
echo "Instance running at $PUBLIC_IP"

SSH="ssh -i $KEY_PATH -o StrictHostKeyChecking=no -o ConnectTimeout=10 ec2-user@$PUBLIC_IP"

# ---------------------------------------------------------------------------
# Wait for SSH to become available
# ---------------------------------------------------------------------------
echo "Waiting for SSH to become available..."
for i in $(seq 1 30); do
  if $SSH "echo ok" &>/dev/null; then
    echo "SSH is up."
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "ERROR: SSH never became available."
    exit 1
  fi
  sleep 10
done

# ---------------------------------------------------------------------------
# Package smolvm source for upload
# ---------------------------------------------------------------------------
echo ""
echo "--- Packaging smolvm source ---"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE_TAR="$(mktemp -d)/smolvm-src.tar.gz"
(cd "$REPO_ROOT" && git archive --format=tar.gz HEAD -- src pyproject.toml README.md > "$SOURCE_TAR")
echo "Packaged source: $(basename "$SOURCE_TAR")"

# ---------------------------------------------------------------------------
# Install dependencies on the instance
# ---------------------------------------------------------------------------
echo ""
echo "--- Installing dependencies ---"
$SSH bash <<'REMOTE'
set -euo pipefail

# Enable KVM access
sudo usermod -aG kvm ec2-user || true
sudo chmod 666 /dev/kvm

# Raise open-file and process limits for high-density VM runs
echo "ec2-user soft nofile 65536" | sudo tee -a /etc/security/limits.conf
echo "ec2-user hard nofile 65536" | sudo tee -a /etc/security/limits.conf
echo "ec2-user soft nproc  65536" | sudo tee -a /etc/security/limits.conf
echo "ec2-user hard nproc  65536" | sudo tee -a /etc/security/limits.conf
# Allow a large number of concurrent TCP connections
sudo sysctl -w net.ipv4.ip_local_port_range="1024 65535" >/dev/null
sudo sysctl -w net.core.somaxconn=65535 >/dev/null

# Disable firewalld — it uses the nftables backend and can conflict with
# SmolVM's own nftables rules, blocking TAP device forwarding.
sudo systemctl stop firewalld 2>/dev/null || true
sudo systemctl disable firewalld 2>/dev/null || true

# Install Python 3.11, pip, and Docker
sudo dnf install -y python3.11 python3.11-pip docker nftables --quiet
sudo systemctl start docker
sudo usermod -aG docker ec2-user

# Mount NVMe instance store and redirect all smolvm data to it (if present)
if test -b /dev/nvme1n1; then
  sudo mkfs.ext4 -F /dev/nvme1n1
  sudo mkdir -p /mnt/nvme
  sudo mount /dev/nvme1n1 /mnt/nvme
  sudo mkdir -p /mnt/nvme/smolvm-images /mnt/nvme/smolvm-state
  sudo chown ec2-user:ec2-user /mnt/nvme /mnt/nvme/smolvm-images /mnt/nvme/smolvm-state
  # Image cache (~/.smolvm/images/) → NVMe
  ln -sfn /mnt/nvme/smolvm-images /home/ec2-user/.smolvm
  # Disk clones (~/.local/state/smolvm/disks/) → NVMe
  mkdir -p /home/ec2-user/.local/state
  ln -sfn /mnt/nvme/smolvm-state /home/ec2-user/.local/state/smolvm
else
  echo "WARNING: /dev/nvme1n1 not found; using root volume for SmolVM data."
fi
REMOTE

scp -i "$KEY_PATH" -o StrictHostKeyChecking=no \
  "$SOURCE_TAR" \
  "ec2-user@$PUBLIC_IP:~/smolvm-src.tar.gz"

$SSH bash <<'REMOTE'
set -euo pipefail

# Install smolvm from source and psutil
pip3.11 install --quiet ~/smolvm-src.tar.gz psutil

# Download Firecracker binary
python3.11 -c "from smolvm.host import HostManager; HostManager().install_firecracker()"
REMOTE

rm -rf "$(dirname "$SOURCE_TAR")"
echo "Dependencies installed."

# ---------------------------------------------------------------------------
# Upload benchmark script
# ---------------------------------------------------------------------------
echo ""
echo "--- Uploading benchmark ---"
scp -i "$KEY_PATH" -o StrictHostKeyChecking=no \
  "$SCRIPT_DIR/density_ramp.py" \
  "ec2-user@$PUBLIC_IP:~/density_ramp.py"
echo "Uploaded density_ramp.py"

# ---------------------------------------------------------------------------
# Run benchmark
# ---------------------------------------------------------------------------
REMOTE_OUTPUT="density.json"
BENCHMARK_CMD="python3.11 ~/density_ramp.py \
  --tier $TIER \
  --max-attempts $MAX_ATTEMPTS \
  --sustain-sec $SUSTAIN_SEC \
  --parallel $PARALLEL \
  ${SHARED_DISK} \
  --output ~/$REMOTE_OUTPUT"

echo ""
echo "--- Running benchmark ---"
echo "  tier=$TIER  max-attempts=$MAX_ATTEMPTS  sustain-sec=$SUSTAIN_SEC  parallel=$PARALLEL  ${SHARED_DISK:+shared-disk}"
echo ""

# Run with a pseudo-TTY so output streams live; failure here still triggers cleanup
$SSH -t "$BENCHMARK_CMD" || {
  echo ""
  echo "WARNING: Benchmark exited with a non-zero status (may be expected at resource limit)."
}

# ---------------------------------------------------------------------------
# Post-run diagnostics (network state + first VM log)
# ---------------------------------------------------------------------------
echo ""
echo "--- Post-run diagnostics ---"
$SSH bash <<'DIAG' || true
set -uo pipefail

echo "=== Network interfaces (TAP devices) ==="
ip -o link show | grep -E "tap|lo|eth" || true

echo ""
echo "=== Routes ==="
ip route show

echo ""
echo "=== nftables ruleset ==="
sudo nft list ruleset 2>/dev/null || echo "(nft list failed)"

echo ""
echo "=== Most recent Firecracker VM log (last 60 lines) ==="
LOG=$(ls -t ~/.local/state/smolvm/vm-*.log 2>/dev/null | head -1)
if [ -n "$LOG" ]; then
  echo "Log: $LOG"
  tail -60 "$LOG"
else
  echo "(no VM log files found under ~/.local/state/smolvm/)"
fi
DIAG

# ---------------------------------------------------------------------------
# Collect results
# ---------------------------------------------------------------------------
echo ""
echo "--- Collecting results ---"
scp -i "$KEY_PATH" -o StrictHostKeyChecking=no \
  "ec2-user@$PUBLIC_IP:~/$REMOTE_OUTPUT" \
  "./$OUTPUT_FILE"

echo "Results saved to: $OUTPUT_FILE"
echo ""
echo "Quick summary:"
python3 - "$OUTPUT_FILE" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    data = json.load(f)

boots = [d for d in data if d.get("event") == "boot_ok"]
fails = [d for d in data if d.get("event") == "boot_fail"]
sustain = next((d for d in data if d.get("event") == "sustain_check"), None)

print(f"  Successful boots : {len(boots)}")
print(f"  Failed boots     : {len(fails)}")
if boots:
    times = [d["boot_time_s"] for d in boots]
    print(f"  Boot time avg    : {sum(times)/len(times):.2f}s")
    print(f"  Boot time max    : {max(times):.2f}s")
    last = boots[-1]
    print(f"  Peak mem used    : {last['host_mem_used_gb']:.2f} GB")
if sustain:
    print(f"  Sustain alive    : {sustain['vms_alive']}/{sustain['vms_checked']} VMs")
PYEOF

# Instance is terminated by the cleanup trap on EXIT
