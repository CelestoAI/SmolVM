#!/usr/bin/env bash
# run_density_ramp_gcp.sh
#
# Launches a GCP instance, deploys and runs the density_ramp benchmark,
# collects results, then deletes the instance.
#
# Prerequisites:
#   - gcloud CLI configured (gcloud auth login && gcloud config set project PROJECT)
#   - A GCP project with the Compute Engine API enabled
#
# Usage:
#   ./run_density_ramp_gcp.sh [--tier tiny|small|med] [--max-attempts N] \
#                              [--sustain-sec N] [--parallel N] [--shared-disk] \
#                              [--output FILE]
#
# Environment variable overrides:
#   PROJECT         GCP project ID (required if not set in gcloud config)
#   ZONE            GCP zone (default: us-central1-a)
#   MACHINE_TYPE    GCP machine type (default: n2-standard-16)
#   IMAGE_FAMILY    OS image family (default: debian-12)
#   IMAGE_PROJECT   OS image project (default: debian-cloud)
#   FIREWALL_RULE   Firewall rule name (optional; creates a temporary one if unset)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (override via environment variables)
# ---------------------------------------------------------------------------
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
ZONE="${ZONE:-us-central1-a}"
MACHINE_TYPE="${MACHINE_TYPE:-n2-standard-16}"
IMAGE_FAMILY="${IMAGE_FAMILY:-debian-12}"
IMAGE_PROJECT="${IMAGE_PROJECT:-debian-cloud}"
FIREWALL_RULE="${FIREWALL_RULE:-}"

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
if ! command -v gcloud &>/dev/null; then
  echo "ERROR: gcloud CLI not found. Install it: https://cloud.google.com/sdk/docs/install"
  exit 1
fi

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: GCP project is not set."
  echo "  export PROJECT=my-gcp-project"
  echo "  or: gcloud config set project my-gcp-project"
  exit 1
fi

# ---------------------------------------------------------------------------
# Optionally create a temporary firewall rule allowing SSH
# ---------------------------------------------------------------------------
INSTANCE_NAME="smolvm-bench-$(date +%s)"
NETWORK_TAG="$INSTANCE_NAME"
CREATED_FIREWALL_RULE=""

if [[ -z "$FIREWALL_RULE" ]]; then
  FIREWALL_RULE="$NETWORK_TAG-ssh"
  echo "Creating temporary firewall rule $FIREWALL_RULE..."
  gcloud compute firewall-rules create "$FIREWALL_RULE" \
    --project="$PROJECT" \
    --direction=INGRESS \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges=0.0.0.0/0 \
    --target-tags="$NETWORK_TAG" \
    --quiet
  CREATED_FIREWALL_RULE="$FIREWALL_RULE"
  echo "Created firewall rule: $FIREWALL_RULE"
fi

# ---------------------------------------------------------------------------
# Launch instance
# ---------------------------------------------------------------------------
INSTANCE_CREATED=0

cleanup() {
  echo ""
  echo "--- Cleanup ---"

  if [[ "$INSTANCE_CREATED" -eq 1 ]]; then
    echo "Deleting instance $INSTANCE_NAME..."
    gcloud compute instances delete "$INSTANCE_NAME" \
      --project="$PROJECT" \
      --zone="$ZONE" \
      --quiet 2>/dev/null || true
    echo "Instance deleted."
  fi

  if [[ -n "$CREATED_FIREWALL_RULE" ]]; then
    echo "Deleting firewall rule $CREATED_FIREWALL_RULE..."
    gcloud compute firewall-rules delete "$CREATED_FIREWALL_RULE" \
      --project="$PROJECT" \
      --quiet 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo ""
echo "Launching $MACHINE_TYPE instance ($INSTANCE_NAME)..."

# --enable-nested-virtualization is required for KVM on GCP
gcloud compute instances create "$INSTANCE_NAME" \
  --project="$PROJECT" \
  --zone="$ZONE" \
  --machine-type="$MACHINE_TYPE" \
  --image-family="$IMAGE_FAMILY" \
  --image-project="$IMAGE_PROJECT" \
  --boot-disk-size=100GB \
  --boot-disk-type=pd-ssd \
  --enable-nested-virtualization \
  --tags="$NETWORK_TAG" \
  --quiet

INSTANCE_CREATED=1
echo "Instance created: $INSTANCE_NAME"

# ---------------------------------------------------------------------------
# Wait for instance to be running
# ---------------------------------------------------------------------------
echo "Waiting for instance to reach RUNNING state..."
for i in $(seq 1 30); do
  STATUS=$(gcloud compute instances describe "$INSTANCE_NAME" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --format="value(status)")
  if [[ "$STATUS" == "RUNNING" ]]; then
    echo "Instance is running."
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "ERROR: Instance never reached RUNNING state (last status: $STATUS)."
    exit 1
  fi
  sleep 5
done

# ---------------------------------------------------------------------------
# Wait for SSH to become available
# ---------------------------------------------------------------------------
echo "Waiting for SSH to become available..."
for i in $(seq 1 30); do
  if gcloud compute ssh "$INSTANCE_NAME" \
      --project="$PROJECT" \
      --zone="$ZONE" \
      --strict-host-key-checking=no \
      --command="echo ok" &>/dev/null; then
    echo "SSH is up."
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "ERROR: SSH never became available."
    exit 1
  fi
  sleep 10
done

GSSH=(gcloud compute ssh "$INSTANCE_NAME"
  --project="$PROJECT"
  --zone="$ZONE"
  --strict-host-key-checking=no
)
GSCP=(gcloud compute scp
  --project="$PROJECT"
  --zone="$ZONE"
  --strict-host-key-checking=no
)

# ---------------------------------------------------------------------------
# Install dependencies on the instance
# ---------------------------------------------------------------------------
echo ""
echo "--- Installing dependencies ---"
"${GSSH[@]}" --command="bash -s" <<'REMOTE'
set -euo pipefail

# Enable KVM access
sudo usermod -aG kvm "$USER" || true
sudo chmod 666 /dev/kvm

# Install Python and pip
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip

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
"${GSCP[@]}" \
  "$SCRIPT_DIR/density_ramp.py" \
  "${INSTANCE_NAME}:~/density_ramp.py"
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
"${GSSH[@]}" --command="$BENCHMARK_CMD" -- -t || {
  echo ""
  echo "WARNING: Benchmark exited with a non-zero status (may be expected at resource limit)."
}

# ---------------------------------------------------------------------------
# Collect results
# ---------------------------------------------------------------------------
echo ""
echo "--- Collecting results ---"
"${GSCP[@]}" \
  "${INSTANCE_NAME}:~/$REMOTE_OUTPUT" \
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

# Instance is deleted by the cleanup trap on EXIT
