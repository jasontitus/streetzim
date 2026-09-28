#!/bin/bash
# Rsync from all running terrain instances every 20 minutes
KEY="$HOME/.ssh/terrain-gen.pem"
LOCAL_DIR="/Users/jasontitus/experiments/streetzim/terrain_cache"

while true; do
    echo "=== $(date) ==="
    IPS=$(aws ec2 describe-instances --region us-east-1 \
      --filters "Name=tag:Name,Values=terrain-gen-*,terrain-split-*" "Name=instance-state-name,Values=running" \
      --query 'Reservations[].Instances[].PublicIpAddress' --output text)
    
    COUNT=$(echo "$IPS" | wc -w)
    if [ "$COUNT" -eq 0 ]; then
        echo "No running instances. Done!"
        break
    fi
    
    echo "Syncing from $COUNT instances..."
    for ip in $IPS; do
        rsync -az -e "ssh -i $KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
            "ubuntu@${ip}:terrain_tiles/12/" "${LOCAL_DIR}/12/" 2>/dev/null &
    done
    wait
    
    LOCAL_COUNT=$(find "${LOCAL_DIR}/12" -name "*.webp" | wc -l)
    echo "Local z12 tiles: $LOCAL_COUNT"
    echo "Sleeping 20 minutes..."
    sleep 1200
done
