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
REGION="${REGION:-us-east-1}"
INSTANCE_TYPE="${INSTANCE_TYPE:-c5.metal}"
KEY_NAME="${KEY_NAME:-}"
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

  # Allow SSH from anywhere (scope this down if your environment allows it)
  aws ec2 authorize-security-group-ingress \
    --region "$REGION" \
    --group-id "$SECURITY_GROUP" \
    --protocol tcp \
    --port 22 \
    --cidr 0.0.0.0/0 \
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
# Install dependencies on the instance
# ---------------------------------------------------------------------------
echo ""
echo "--- Installing dependencies ---"
$SSH bash <<'REMOTE'
set -euo pipefail

# Enable KVM access
sudo usermod -aG kvm ec2-user || true
sudo chmod 666 /dev/kvm

# Install Python and pip
sudo dnf install -y python3 python3-pip --quiet

# Install smolvm and psutil
pip3 install --quiet smolvm psutil

# Trigger Firecracker binary download
smolvm demo list 2>/dev/null || true
REMOTE

echo "Dependencies installed."

# ---------------------------------------------------------------------------
# Upload benchmark script
# ---------------------------------------------------------------------------
echo ""
echo "--- Uploading benchmark ---"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
scp -i "$KEY_PATH" -o StrictHostKeyChecking=no \
  "$SCRIPT_DIR/density_ramp.py" \
  "ec2-user@$PUBLIC_IP:~/density_ramp.py"
echo "Uploaded density_ramp.py"

# ---------------------------------------------------------------------------
# Run benchmark
# ---------------------------------------------------------------------------
REMOTE_OUTPUT="density.json"
BENCHMARK_CMD="python3 ~/density_ramp.py \
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
