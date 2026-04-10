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
    # Check VM status
    STATUS=$(gcloud compute instances describe "$VM" --project="$PROJECT" \
      --format="value(status)" 2>/dev/null || echo "DELETED")

    case "$STATUS" in
      RUNNING)
        any_alive=1
        ;;
      TERMINATED|STOPPED)
        # VM stopped itself — check if it has the phase marker
        ZONE=$(gcloud compute instances describe "$VM" --project="$PROJECT" \
          --format="value(zone)" 2>/dev/null | awk -F/ '{print $NF}')

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
        # VM self-deleted after successful build — done!
        echo "[$(date '+%H:%M:%S')] $VM is gone (build complete or manually deleted)."
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
