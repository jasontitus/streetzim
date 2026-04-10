#!/bin/bash
# Watch for spot VMs that stopped themselves before ZIM packaging,
# convert them to on-demand, and restart. The startup script on the VM
# detects it's now STANDARD (not spot) and runs the full build — with
# satellite/terrain already cached, it goes straight to ZIM packaging.
#
# Usage: bash cloud/spot-to-ondemand-watcher.sh
# Runs until all watched VMs are done (self-deleted after build complete).
set -euo pipefail

PROJECT=streetzim
WATCHED_VMS=("streetzim-build-africa" "streetzim-build-asia")

echo "=== Spot-to-on-demand watcher started: $(date) ==="
echo "Watching: ${WATCHED_VMS[*]}"
echo "Polling every 60 seconds for TERMINATED VMs..."
echo ""

while true; do
  any_alive=0
  for VM in "${WATCHED_VMS[@]}"; do
    # Check VM status + zone via list (doesn't require --zone)
    VM_INFO=$(gcloud compute instances list --project="$PROJECT" \
      --filter="name=$VM" --format="value(status,zone)" 2>/dev/null)
    STATUS=$(echo "$VM_INFO" | awk '{print $1}')
    ZONE=$(echo "$VM_INFO" | awk '{print $2}' | awk -F/ '{print $NF}')
    [ -z "$STATUS" ] && STATUS="DELETED"

    case "$STATUS" in
      RUNNING)
        any_alive=1
        ;;
      TERMINATED|STOPPED)

        echo "[$(date '+%H:%M:%S')] $VM is $STATUS in $ZONE — converting to on-demand..."

        # Switch provisioning model to STANDARD (on-demand)
        gcloud compute instances set-scheduling "$VM" \
          --zone="$ZONE" --project="$PROJECT" \
          --provisioning-model=STANDARD --quiet 2>&1 || true

        # Restart
        echo "[$(date '+%H:%M:%S')] Starting $VM as on-demand..."
        gcloud compute instances start "$VM" \
          --zone="$ZONE" --project="$PROJECT" --quiet 2>&1

        echo "[$(date '+%H:%M:%S')] $VM restarted as on-demand for ZIM packaging."
        any_alive=1
        ;;
      DELETED|"")
        # VM is gone. Could be: (a) successful build + self-delete, or
        # (b) spot preemption. Check Archive.org for the finished ZIM to
        # decide. If not uploaded yet, it was preempted — relaunch.
        REGION_ID="${VM#streetzim-build-}"
        IA_CHECK=$(curl -sf "https://archive.org/metadata/streetzim-${REGION_ID}" 2>/dev/null \
          | python3 -c "import sys,json; d=json.load(sys.stdin); fs=[f for f in d.get('files',[]) if f.get('name','').endswith('.zim')]; print('YES' if fs and int(fs[0].get('size',0))>1000000 else 'NO')" 2>/dev/null || echo "NO")

        if [ "$IA_CHECK" = "YES" ]; then
          echo "[$(date '+%H:%M:%S')] $VM: build complete (ZIM found on Archive.org). Done."
        else
          echo "[$(date '+%H:%M:%S')] $VM: preempted (no ZIM on Archive.org). Relaunching..."
          LAUNCH_DIR="$(dirname "$0")"
          bash "$LAUNCH_DIR/launch-build-vm.sh" "$REGION_ID" --fast --spot 2>&1 | tail -3
          any_alive=1
        fi
        ;;
      *)
        echo "[$(date '+%H:%M:%S')] $VM: unexpected status '$STATUS'"
        any_alive=1
        ;;
    esac
  done

  if [ $any_alive -eq 0 ]; then
    echo ""
    echo "=== All watched VMs are done. Watcher exiting. ==="
    break
  fi

  sleep 60
done
