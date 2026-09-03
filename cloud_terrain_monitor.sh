#!/bin/bash
# Monitor cloud terrain instances, collect tiles as they finish, terminate completed ones.
# Run this in a loop: ./cloud_terrain_monitor.sh
# Or with watch: watch -n 60 ./cloud_terrain_monitor.sh

KEY="$HOME/.ssh/terrain-gen.pem"
REGION="us-east-1"
LOCAL_DIR="/Users/jasontitus/experiments/streetzim/terrain_cache"
TAG="terrain-gen"

IPS=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=${TAG}-*" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[Tags[?Key==`Name`].Value|[0],InstanceId,PublicIpAddress]' \
  --output text | sort)

if [ -z "$IPS" ]; then
    echo "No running instances."
    exit 0
fi

RUNNING=0
DONE=0
COLLECTING=0

while IFS=$'\t' read -u 9 -r name iid ip; do
    [ -z "$ip" ] && continue
    # Check if done
    status=$(ssh -n -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
        ubuntu@$ip 'grep -c "^DONE" gen.log 2>/dev/null || echo 0' 2>/dev/null)

    if [ "$status" = "1" ]; then
        DONE=$((DONE+1))
        tiles=$(ssh -n -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
            ubuntu@$ip 'find terrain_tiles/12 -name "*.webp" 2>/dev/null | wc -l' 2>/dev/null || echo "?")
        printf "%-14s DONE (%s tiles) — collecting...\n" "$name" "$tiles"

        # Rsync tiles back; keep stderr visible so transfer failures are
        # actionable in logs.
        rsync -az -e "ssh -i $KEY -o StrictHostKeyChecking=no" \
            "ubuntu@${ip}:terrain_tiles/12/" "${LOCAL_DIR}/12/"

        if [ $? -eq 0 ]; then
            verified=true
            sample=$(ssh -n -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
                ubuntu@$ip 'find terrain_tiles/12 -name "*.webp" 2>/dev/null | shuf -n 5' 2>/dev/null || true)
            if [ -z "$sample" ]; then
                printf "%-14s Verify failed: no remote sample tiles; keeping instance\n" "$name"
                verified=false
            fi
            for remote_path in $sample; do
                local_path="${LOCAL_DIR}/${remote_path#terrain_tiles/}"
                if [ ! -f "$local_path" ]; then
                    printf "%-14s Verify failed: %s missing locally; keeping instance\n" "$name" "$local_path"
                    verified=false
                    break
                fi
            done
            if [ "$verified" = true ]; then
                printf "%-14s Collected and verified! Terminating %s\n" "$name" "$iid"
                aws ec2 terminate-instances --region "$REGION" --instance-ids "$iid" > /dev/null 2>&1
            fi
        else
            printf "%-14s Rsync failed, keeping instance\n" "$name"
        fi
    else
        RUNNING=$((RUNNING+1))
        progress=$(ssh -n -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
            ubuntu@$ip 'tail -c 120 gen.log 2>/dev/null | tr "\r" "\n" | grep -v "^$" | tail -1' 2>/dev/null || echo "starting...")
        printf "%-14s %s\n" "$name" "$progress"
    fi
done 9<<< "$IPS"

echo ""
echo "Running: $RUNNING, Done+collected: $DONE"
