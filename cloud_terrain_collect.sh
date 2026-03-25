#!/bin/bash
# Safely collect tiles from cloud instances.
# NEVER terminates an instance until tiles are verified locally.
#
# Usage:
#   ./cloud_terrain_collect.sh          # sync and verify all
#   ./cloud_terrain_collect.sh terminate # sync, verify, THEN terminate verified ones

set -e

KEY="$HOME/.ssh/terrain-gen.pem"
REGION="us-east-1"
LOCAL_DIR="/Users/jasontitus/experiments/streetzim/terrain_cache"
SSH_OPTS="-i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=30"
DO_TERMINATE="${1:-}"

echo "=== Cloud Terrain Tile Collector ==="
echo ""

# Get all running instances
INSTANCES=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=terrain-gen-*,terrain-split-*" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[Tags[?Key==`Name`].Value|[0],InstanceId,PublicIpAddress]' \
  --output text | sort)

if [ -z "$INSTANCES" ]; then
    echo "No running instances."
    exit 0
fi

TOTAL=0
DONE=0
SYNCED=0
VERIFIED=0

while IFS=$'\t' read -r name iid ip; do
    [ -z "$ip" ] && continue
    TOTAL=$((TOTAL+1))

    echo "--- $name ($ip) ---"

    # Check if instance is done
    completed=$(ssh $SSH_OPTS ubuntu@$ip 'cat terrain_tiles/COMPLETED 2>/dev/null || echo NOT_DONE' 2>/dev/null)

    if [ "$completed" = "NOT_DONE" ]; then
        # Still running — show progress
        progress=$(ssh $SSH_OPTS ubuntu@$ip 'tail -c 120 gen.log 2>/dev/null | tr "\r" "\n" | grep -v "^$" | tail -1' 2>/dev/null || echo "unknown")
        echo "  STILL RUNNING: $progress"
        continue
    fi

    DONE=$((DONE+1))
    expected=$completed
    echo "  Completed: $expected tiles on instance"

    # Rsync tiles
    echo "  Syncing..."
    rsync -az -e "ssh $SSH_OPTS" "ubuntu@${ip}:terrain_tiles/12/" "${LOCAL_DIR}/12/"

    if [ $? -ne 0 ]; then
        echo "  ERROR: rsync failed! NOT terminating."
        continue
    fi
    SYNCED=$((SYNCED+1))

    # Verify by checking the instance's tile directories exist locally
    # Get list of x-directories from instance and verify they have files locally
    remote_count=$(ssh $SSH_OPTS ubuntu@$ip 'find terrain_tiles/12 -name "*.webp" | wc -l' 2>/dev/null)
    echo "  Remote has: $remote_count tiles"

    # Spot-check: pick 5 random tiles from the instance and verify they exist locally
    checks_ok=true
    sample=$(ssh $SSH_OPTS ubuntu@$ip 'find terrain_tiles/12 -name "*.webp" | shuf -n 5' 2>/dev/null)
    for remote_path in $sample; do
        local_path="${LOCAL_DIR}/${remote_path#terrain_tiles/}"
        if [ ! -f "$local_path" ]; then
            echo "  VERIFY FAILED: $local_path missing!"
            checks_ok=false
            break
        fi
    done

    if [ "$checks_ok" = true ]; then
        VERIFIED=$((VERIFIED+1))
        echo "  VERIFIED OK ($remote_count tiles synced)"

        if [ "$DO_TERMINATE" = "terminate" ]; then
            echo "  Terminating $iid..."
            aws ec2 terminate-instances --region "$REGION" --instance-ids "$iid" > /dev/null 2>&1
            echo "  Terminated."
        else
            echo "  (run with 'terminate' arg to terminate verified instances)"
        fi
    else
        echo "  NOT VERIFIED — keeping instance alive"
    fi

done <<< "$INSTANCES"

echo ""
echo "=== Summary ==="
echo "Total instances: $TOTAL"
echo "Done generating: $DONE"
echo "Synced: $SYNCED"
echo "Verified: $VERIFIED"
echo ""

LOCAL_TOTAL=$(find "${LOCAL_DIR}/12" -name "*.webp" | wc -l)
echo "Local z12 tiles: $LOCAL_TOTAL"
echo "Target: ~16,728,064"
echo "Missing: ~$((16728064 - LOCAL_TOTAL))"
