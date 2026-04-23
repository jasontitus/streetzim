#!/bin/bash
# Cron-safe VM health check. Runs every 5 minutes via crontab.
# - Checks if each expected VM is RUNNING with a live build process
# - If VM is gone (preempted): relaunches as spot
# - If VM is RUNNING but build dead: deletes and relaunches
# - If VM is TERMINATED (self-stopped for packaging): converts to on-demand
# - Logs to cloud/vm-health.log
#
# Install: crontab -e → */5 * * * * bash /Users/jasontitus/experiments/streetzim/cloud/vm-health-cron.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT=streetzim
LOG="$SCRIPT_DIR/vm-health.log"
LOCKFILE="$SCRIPT_DIR/.vm-health.lock"
WATCHED_VMS=("streetzim-build-africa" "streetzim-build-asia")

# Prevent overlapping runs (cron fires every 5 min; a slow run shouldn't stack)
if [ -f "$LOCKFILE" ]; then
  LOCK_AGE=$(( $(date +%s) - $(stat -f%m "$LOCKFILE") ))
  if [ $LOCK_AGE -lt 300 ]; then
    exit 0  # Previous run still active
  fi
  # Stale lock (>5 min old) — remove and continue
fi
echo $$ > "$LOCKFILE"
trap "rm -f $LOCKFILE" EXIT

# Only log if something happens (avoid filling log with "all ok" lines)
ACTED=0

for VM in "${WATCHED_VMS[@]}"; do
  VM_INFO=$(gcloud compute instances list --project="$PROJECT" \
    --filter="name=$VM" --format="value(status,zone)" 2>/dev/null)
  STATUS=$(echo "$VM_INFO" | awk '{print $1}')
  ZONE=$(echo "$VM_INFO" | awk '{print $2}' | awk -F/ '{print $NF}')
  [ -z "$STATUS" ] && STATUS="GONE"

  case "$STATUS" in
    RUNNING)
      # Health check: is the build process alive?
      BUILD_ALIVE=$(gcloud compute ssh "$VM" --zone="$ZONE" --project="$PROJECT" \
        --command='pgrep -f "create_osm_zim\|apt-get\|pip install\|git clone\|gcloud storage\|tar " > /dev/null 2>&1 && echo YES || echo NO' 2>/dev/null || echo "SSH_FAIL")
      if [ "$BUILD_ALIVE" = "NO" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M')] $VM: RUNNING but build DEAD — deleting and relaunching" >> "$LOG"
        gcloud compute instances delete "$VM" --zone="$ZONE" --project="$PROJECT" --quiet 2>&1 || true
        REGION_ID="${VM#streetzim-build-}"
        bash "$SCRIPT_DIR/launch-build-vm.sh" "$REGION_ID" --fast --spot >> "$LOG" 2>&1 || true
        ACTED=1
      fi
      ;;
    TERMINATED|STOPPED)
      echo "[$(date '+%Y-%m-%d %H:%M')] $VM: TERMINATED — converting to on-demand" >> "$LOG"
      gcloud compute instances set-scheduling "$VM" --zone="$ZONE" --project="$PROJECT" \
        --provisioning-model=STANDARD --quiet 2>&1 || true
      gcloud compute instances start "$VM" --zone="$ZONE" --project="$PROJECT" --quiet 2>&1 || true
      ACTED=1
      ;;
    GONE)
      # Check if build is done (ZIM on Archive.org)
      REGION_ID="${VM#streetzim-build-}"
      IA_CHECK=$(curl -sf "https://archive.org/metadata/streetzim-${REGION_ID}" 2>/dev/null \
        | python3 -c "import sys,json; d=json.load(sys.stdin); fs=[f for f in d.get('files',[]) if f.get('name','').endswith('.zim')]; print('YES' if fs and int(fs[0].get('size',0))>1000000 else 'NO')" 2>/dev/null || echo "NO")
      if [ "$IA_CHECK" = "YES" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M')] $VM: done (ZIM on Archive.org)" >> "$LOG"
      else
        echo "[$(date '+%Y-%m-%d %H:%M')] $VM: GONE (preempted) — relaunching" >> "$LOG"
        bash "$SCRIPT_DIR/launch-build-vm.sh" "$REGION_ID" --fast --spot >> "$LOG" 2>&1 || true
      fi
      ACTED=1
      ;;
  esac
done

# If all VMs are done (ZIMs on Archive.org), remove this cron job
if [ $ACTED -eq 0 ]; then
  ALL_DONE=1
  for VM in "${WATCHED_VMS[@]}"; do
    REGION_ID="${VM#streetzim-build-}"
    IA_CHECK=$(curl -sf "https://archive.org/metadata/streetzim-${REGION_ID}" 2>/dev/null \
      | python3 -c "import sys,json; d=json.load(sys.stdin); fs=[f for f in d.get('files',[]) if f.get('name','').endswith('.zim')]; print('YES' if fs and int(fs[0].get('size',0))>1000000 else 'NO')" 2>/dev/null || echo "NO")
    [ "$IA_CHECK" != "YES" ] && ALL_DONE=0
  done
  if [ $ALL_DONE -eq 1 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M')] All builds complete. Removing cron job." >> "$LOG"
    crontab -l 2>/dev/null | grep -v vm-health-cron | crontab -
  fi
fi
