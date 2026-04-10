#!/bin/bash
# Launch a GCP build VM for one StreetZim region.
#
# Usage: bash launch-build-vm.sh <region-id> [--fast] [--spot] [--big]
#   region-id: africa | asia | south-america | oceania | world | japan | ...
#   --fast   Use c3-standard-8 (Sapphire Rapids, ~20% faster single-thread,
#            ~50% cheaper than n2-standard-16).
#   --spot   Use a Spot VM (60-80% off). Safe because cache-push-on-trap
#            runs even if the VM is preempted.
#   --big    Force n2-standard-16 (default for continent builds — more
#            cores help terrain regen and satellite download).
#
# Examples:
#   bash launch-build-vm.sh africa --big --spot    # big VM, cheap
#   bash launch-build-vm.sh japan  --fast --spot   # cheap + fast single-thread
set -euo pipefail
cd "$(dirname "$0")"

PROJECT=streetzim

# Parse args
REGION_ID=""
USE_FAST=0
USE_SPOT=0
USE_BIG=0
for arg in "$@"; do
  case "$arg" in
    --fast) USE_FAST=1 ;;
    --spot) USE_SPOT=1 ;;
    --big)  USE_BIG=1 ;;
    --*)    echo "Unknown flag: $arg"; exit 1 ;;
    *)      REGION_ID="$arg" ;;
  esac
done

# Machine type selection. Default to c3-standard-8 (cheaper, faster single-core)
# unless --big is explicitly set. --fast is an alias for the default c3 choice.
if [ $USE_BIG -eq 1 ]; then
  MACHINE_TYPE=n2-standard-16   # 16 vCPU, 64 GB RAM, $0.78/hr on-demand
  ZONES=(us-central1-a us-central1-b us-central1-c us-central1-f us-east1-b us-east1-c us-east1-d us-west1-a us-west1-b us-west1-c)
else
  # n2-standard-8: 8 vCPU, 32 GB RAM, $0.39/hr on-demand
  # (c3 doesn't support pd-standard disks; n2 is compatible and similar price)
  MACHINE_TYPE=n2-standard-8
  ZONES=(us-central1-a us-central1-b us-central1-c us-central1-f us-east1-b us-east1-c us-east1-d us-west1-a us-west1-b us-west1-c)
fi

DISK_SIZE=750                  # GB pd-standard — needs room for:
                               #   ~120 GB world-data (PBF + v2 mbtiles only)
                               #   ~100 GB satellite tarball + extracted
                               #   ~150 GB terrain dem_sources + generated tiles
                               #   ~16 GB search cache
                               #   ~50 GB output ZIM + working space
                               #   = ~440 GB; 750 leaves headroom

if [ -z "$REGION_ID" ]; then
  echo "Usage: $0 <region-id> [--fast] [--spot] [--big]"
  echo "Available regions:"
  echo "  africa, asia, south-america, oceania, world, japan"
  exit 1
fi

# Region definitions
case "$REGION_ID" in
  africa)
    NAME="Africa"
    BBOX="-18.0,-35.0,52.0,38.0"
    DESC="Offline map of Africa, including Algeria, Angola, Benin, Botswana, Burkina Faso, Burundi, Cameroon, Cape Verde, Central African Republic, Chad, Comoros, Democratic Republic of the Congo, Republic of the Congo, Djibouti, Egypt, Equatorial Guinea, Eritrea, Eswatini, Ethiopia, Gabon, The Gambia, Ghana, Guinea, Guinea-Bissau, Ivory Coast, Kenya, Lesotho, Liberia, Libya, Madagascar, Malawi, Mali, Mauritania, Mauritius, Morocco, Mozambique, Namibia, Niger, Nigeria, Rwanda, Sao Tome and Principe, Senegal, Seychelles, Sierra Leone, Somalia, South Africa, South Sudan, Sudan, Tanzania, Togo, Tunisia, Uganda, Zambia, and Zimbabwe."
    ;;
  asia)
    NAME="Asia"
    BBOX="25.0,-12.0,180.0,82.0"
    DESC="Offline map of Asia, including China, Japan, India (and the Indian Subcontinent), Russia (Asian portion), Indonesia, Philippines, Vietnam, Thailand, Malaysia, Singapore, South Korea, North Korea, Mongolia, Kazakhstan, Uzbekistan, Turkmenistan, Kyrgyzstan, Tajikistan, Afghanistan, Iran, Iraq, Saudi Arabia, Yemen, Oman, UAE, Qatar, Bahrain, Kuwait, Jordan, Israel, Lebanon, Syria, Turkey, and many more."
    ;;
  south-america)
    NAME="South America"
    BBOX="-82.0,-56.0,-34.0,13.0"
    DESC="Offline map of South America, including Argentina, Bolivia, Brazil, Chile, Colombia, Ecuador, Guyana, Paraguay, Peru, Suriname, Uruguay, Venezuela, and French Guiana."
    ;;
  oceania)
    NAME="Oceania"
    BBOX="110.0,-50.0,180.0,0.0"
    DESC="Offline map of Oceania, including Australia, New Zealand, Papua New Guinea, Fiji, Solomon Islands, Vanuatu, Samoa, Tonga, Kiribati, Micronesia, Palau, Marshall Islands, Nauru, and Tuvalu."
    ;;
  world)
    NAME="World"
    BBOX="-180.0,-85.0,180.0,85.0"
    DESC="Offline map of the entire world, with detailed street-level coverage of every country."
    ;;
  japan)
    NAME="Japan"
    BBOX="122.9,24.0,146.0,45.6"
    DESC="Offline map of Japan, including all four main islands (Honshu, Hokkaido, Kyushu, and Shikoku), Okinawa, the Izu islands, and the Ryukyu archipelago. Major cities include Tokyo, Osaka, Yokohama, Nagoya, Sapporo, Kyoto, Fukuoka, Kobe, Kawasaki, Saitama, Hiroshima, Sendai, Chiba, Kitakyushu, and Naha. Features Mount Fuji, Japanese Alps, Seto Inland Sea, and all major national parks."
    ;;
  *)
    echo "Unknown region: $REGION_ID"
    exit 1
    ;;
esac

INSTANCE_NAME="streetzim-build-${REGION_ID}"

# Long description with feature list (URL-encoded for metadata)
FULL_DESC="$DESC

This is a complete offline map viewer packaged as a ZIM file for the free Kiwix reader app (iOS, Android, Mac, Windows, Linux). No internet connection required.

Includes vector maps (MapLibre GL JS + OpenMapTiles), Sentinel-2 satellite imagery, Copernicus GLO-30 terrain with hillshade and 3D, Wikipedia/Wikidata place info, and full-text search.

How to use: install Kiwix from https://kiwix.org and open the .zim file.

Data sources: OpenStreetMap (ODbL), OpenMapTiles (CC-BY 4.0), Sentinel-2 cloudless by EOX (CC BY-NC-SA 4.0), Copernicus GLO-30 DEM (ESA/DLR/Airbus), Wikidata (CC0), Wikipedia (CC BY-SA 3.0).

Built with StreetZim: https://github.com/jasontitus/streetzim"

ENC_DESC=$(python3 -c "import sys, urllib.parse; print(urllib.parse.quote(sys.stdin.read()))" <<< "$FULL_DESC")

# Read Archive.org credentials from local config
IA_INI=~/.config/internetarchive/ia.ini
if [ ! -f "$IA_INI" ]; then
  echo "ERROR: $IA_INI not found. Run 'ia configure' first."
  exit 1
fi
IA_ACCESS=$(grep "^access" "$IA_INI" | awk -F= '{print $2}' | tr -d ' ')
IA_SECRET=$(grep "^secret" "$IA_INI" | awk -F= '{print $2}' | tr -d ' ')

SPOT_FLAGS=()
if [ $USE_SPOT -eq 1 ]; then
  SPOT_FLAGS=(--provisioning-model=SPOT --instance-termination-action=DELETE)
fi

echo "=== Launching VM: $INSTANCE_NAME ==="
echo "  Region: $NAME ($BBOX)"
echo "  Machine: $MACHINE_TYPE $([ $USE_SPOT -eq 1 ] && echo '(SPOT)')"
echo "  Disk: ${DISK_SIZE}GB pd-standard"
echo "  Zones to try: ${ZONES[*]}"

# Make sure the VM has a service account that can write to GCS + delete itself
SA="streetzim-builder@${PROJECT}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$SA" --project="$PROJECT" &>/dev/null; then
  echo "Creating service account $SA ..."
  gcloud iam service-accounts create streetzim-builder --project="$PROJECT" \
    --display-name="StreetZim build VM"
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA" \
    --role="roles/storage.objectAdmin" >/dev/null
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA" \
    --role="roles/compute.instanceAdmin.v1" >/dev/null
fi

# gcloud --metadata uses comma as delimiter — but BBOX and DESCRIPTION
# contain commas. Write each value to a temp file and use --metadata-from-file.
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
echo -n "$REGION_ID" > "$TMPDIR/region-id"
echo -n "$NAME"      > "$TMPDIR/region-name"
echo -n "$BBOX"      > "$TMPDIR/region-bbox"
echo -n "$IA_ACCESS" > "$TMPDIR/ia-access-key"
echo -n "$IA_SECRET" > "$TMPDIR/ia-secret-key"
echo -n "$ENC_DESC"  > "$TMPDIR/description"

SUCCESS=0
for ZONE in "${ZONES[@]}"; do
  echo "  Trying zone: $ZONE ..."
  if gcloud compute instances create "$INSTANCE_NAME" \
      --project="$PROJECT" \
      --zone="$ZONE" \
      --machine-type="$MACHINE_TYPE" \
      --image-family=debian-12 --image-project=debian-cloud \
      --boot-disk-size="${DISK_SIZE}GB" \
      --boot-disk-type=pd-standard \
      --service-account="$SA" \
      --scopes=https://www.googleapis.com/auth/cloud-platform \
      "${SPOT_FLAGS[@]}" \
      --metadata-from-file="startup-script=build-vm-startup.sh,region-id=$TMPDIR/region-id,region-name=$TMPDIR/region-name,region-bbox=$TMPDIR/region-bbox,ia-access-key=$TMPDIR/ia-access-key,ia-secret-key=$TMPDIR/ia-secret-key,description=$TMPDIR/description" 2>&1; then
    SUCCESS=1
    LAUNCHED_ZONE=$ZONE
    break
  fi
  echo "    Zone $ZONE unavailable, trying next..."
done

if [ $SUCCESS -eq 0 ]; then
  echo "ERROR: No zones had $MACHINE_TYPE capacity"
  exit 1
fi
ZONE=$LAUNCHED_ZONE

echo ""
echo "VM launched. Tail logs with:"
echo "  gcloud compute ssh $INSTANCE_NAME --zone=$ZONE -- tail -f /var/log/streetzim-build.log"
echo ""
echo "Or watch from console:"
echo "  https://console.cloud.google.com/compute/instances?project=$PROJECT"
