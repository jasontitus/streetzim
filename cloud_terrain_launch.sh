#!/bin/bash
# Launch 8 spot instances to generate world z12 terrain tiles in parallel.
# Each instance gets a latitude band, reads DEMs from S3, generates tiles.
# Run this from your local machine with AWS CLI configured.
#
# Usage:
#   ./cloud_terrain_launch.sh          # launch all 8
#   ./cloud_terrain_launch.sh status   # check progress
#   ./cloud_terrain_launch.sh collect  # rsync results back
#   ./cloud_terrain_launch.sh cleanup  # terminate instances

set -e

INSTANCE_TYPE="c7g.2xlarge"
AMI="ami-0bc0f64eea5d47edf"  # Ubuntu 24.04 ARM64 (us-east-1)
KEY_NAME="${AWS_KEY_NAME:-terrain-gen}"
SECURITY_GROUP="${AWS_SG:-sg-09da6ea5b0a7189f0}"
REGION="us-east-1"
WORKERS=6  # per instance (leave 2 cores for OS on 8-core machine)
TAG="terrain-gen"

# 16 latitude bands covering -85 to 85
# Bands are sized roughly proportional to land area
BANDS=(
  "-85,-60"   # Band 0: Antarctica
  "-60,-45"   # Band 1: southern ocean/Patagonia
  "-45,-33"   # Band 2: southern S.America/Australia
  "-33,-20"   # Band 3: S.Africa/Brazil/Australia
  "-20,-10"   # Band 4: south tropics
  "-10,0"     # Band 5: equatorial south
  "0,10"      # Band 6: equatorial north
  "10,20"     # Band 7: north tropics
  "20,28"     # Band 8: subtropics south
  "28,35"     # Band 9: subtropics north
  "35,42"     # Band 10: Mediterranean/Central Asia
  "42,48"     # Band 11: Europe/Central Asia
  "48,55"     # Band 12: N.Europe/Russia
  "55,62"     # Band 13: Scandinavia/Russia
  "62,72"     # Band 14: Arctic
  "72,85"     # Band 15: High Arctic
)

SETUP_SCRIPT='#!/bin/bash
yum install -y python3-pip gdal gdal-devel
pip3 install rasterio mercantile Pillow numpy
'

launch() {
    echo "Launching 8 spot instances..."
    for i in "${!BANDS[@]}"; do
        IFS=',' read -r lat_min lat_max <<< "${BANDS[$i]}"
        BBOX="-180,${lat_min},180,${lat_max}"

        USERDATA=$(cat <<SCRIPT
#!/bin/bash
exec > /home/ubuntu/setup.log 2>&1
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv
python3 -m venv /home/ubuntu/venv
/home/ubuntu/venv/bin/pip install rasterio mercantile Pillow numpy
echo "SETUP DONE"

cat > /home/ubuntu/gen.py << 'PYSCRIPT'
$(cat cloud_terrain_gen.py)
PYSCRIPT

chown ubuntu:ubuntu /home/ubuntu/gen.py
cd /home/ubuntu
su ubuntu -c "/home/ubuntu/venv/bin/python3 gen.py --zoom 12 --bbox='${BBOX}' --workers=${WORKERS} --output terrain_tiles/ > gen.log 2>&1"
echo "DONE" >> /home/ubuntu/gen.log
SCRIPT
)

        INSTANCE_ID=$(aws ec2 run-instances \
            --region "$REGION" \
            --instance-type "$INSTANCE_TYPE" \
            --image-id "$AMI" \
            --key-name "$KEY_NAME" \
            --security-group-ids "$SECURITY_GROUP" \
            --instance-market-options '{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"one-time"}}' \
            --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":200,"VolumeType":"gp3"}}]' \
            --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${TAG}-${i}}]" \
            --user-data "$USERDATA" \
            --query 'Instances[0].InstanceId' \
            --output text)

        echo "  Band $i ($BBOX): $INSTANCE_ID"
    done
    echo ""
    echo "All launched. Check progress with: $0 status"
}

status() {
    echo "Instance status:"
    aws ec2 describe-instances \
        --region "$REGION" \
        --filters "Name=tag:Name,Values=${TAG}-*" "Name=instance-state-name,Values=running,pending" \
        --query 'Reservations[].Instances[].[Tags[?Key==`Name`].Value|[0],InstanceId,PublicIpAddress,State.Name]' \
        --output table

    echo ""
    echo "Checking progress on each instance..."
    IPS=$(aws ec2 describe-instances \
        --region "$REGION" \
        --filters "Name=tag:Name,Values=${TAG}-*" "Name=instance-state-name,Values=running" \
        --query 'Reservations[].Instances[].[Tags[?Key==`Name`].Value|[0],PublicIpAddress]' \
        --output text)

    echo ""
    printf "%-16s %-16s %s\n" "INSTANCE" "IP" "PROGRESS"
    printf "%-16s %-16s %s\n" "--------" "--" "--------"
    while IFS=$'\t' read -r name ip; do
        if [ -n "$ip" ] && [ "$ip" != "None" ]; then
            progress=$(ssh -i ~/.ssh/${KEY_NAME}.pem -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
                ubuntu@${ip} 'tail -c 200 gen.log 2>/dev/null | tr "\r" "\n" | grep -v "^$" | tail -1' 2>/dev/null || echo "connecting...")
            printf "%-16s %-16s %s\n" "$name" "$ip" "$progress"
        fi
    done <<< "$IPS"

    echo ""
    echo "Tile counts on each instance:"
    while IFS=$'\t' read -r name ip; do
        if [ -n "$ip" ] && [ "$ip" != "None" ]; then
            count=$(ssh -i ~/.ssh/${KEY_NAME}.pem -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
                ubuntu@${ip} 'find terrain_tiles/12 -name "*.webp" 2>/dev/null | wc -l' 2>/dev/null || echo "?")
            printf "  %-16s %s tiles\n" "$name" "$count"
        fi
    done <<< "$IPS"
}

collect() {
    echo "Collecting tiles from all instances..."
    LOCAL_DIR="/Users/jasontitus/experiments/streetzim/terrain_cache"

    IPS=$(aws ec2 describe-instances \
        --region "$REGION" \
        --filters "Name=tag:Name,Values=${TAG}-*" "Name=instance-state-name,Values=running" \
        --query 'Reservations[].Instances[].PublicIpAddress' \
        --output text)

    for IP in $IPS; do
        echo "  Syncing from $IP..."
        rsync -avz --progress \
            -e "ssh -i ~/.ssh/${KEY_NAME}.pem -o StrictHostKeyChecking=no" \
            "ubuntu@${IP}:terrain_tiles/12/" \
            "${LOCAL_DIR}/12/" &
    done
    wait
    echo "All syncs complete!"
}

cleanup() {
    echo "Terminating all terrain-gen instances..."
    INSTANCE_IDS=$(aws ec2 describe-instances \
        --region "$REGION" \
        --filters "Name=tag:Name,Values=${TAG}-*" "Name=instance-state-name,Values=running,pending" \
        --query 'Reservations[].Instances[].InstanceId' \
        --output text)

    if [ -n "$INSTANCE_IDS" ]; then
        aws ec2 terminate-instances --region "$REGION" --instance-ids $INSTANCE_IDS
        echo "Terminated: $INSTANCE_IDS"
    else
        echo "No running instances found."
    fi
}

case "${1:-launch}" in
    launch)  launch ;;
    status)  status ;;
    collect) collect ;;
    cleanup) cleanup ;;
    *)       echo "Usage: $0 {launch|status|collect|cleanup}" ;;
esac
