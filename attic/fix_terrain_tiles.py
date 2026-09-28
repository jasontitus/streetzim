#!/usr/bin/env python3
"""Delete broken (stub) terrain tiles that are over land, then regenerate them."""
import os, math, multiprocessing, sys

CACHE = 'terrain_cache'
DEM_DIR = os.path.join(CACHE, 'dem_sources')

# Build set of (lat, lon) cells that have DEM data (i.e., land)
land_cells = set()
for f in os.listdir(DEM_DIR):
    if not f.startswith('dem_') or not f.endswith('.tif'):
        continue
    if os.path.getsize(os.path.join(DEM_DIR, f)) < 1000:
        continue
    parts = f.replace('dem_', '').replace('.tif', '').split('_')
    if len(parts) != 2:
        continue
    try:
        lat = int(parts[0][1:]) * (1 if parts[0][0] == 'N' else -1)
        lon = int(parts[1][1:]) * (1 if parts[1][0] == 'E' else -1)
        land_cells.add((lat, lon))
    except:
        continue

print(f'{len(land_cells)} DEM cells with land data')


def scan_zoom(z):
    """Scan one zoom level for broken tiles over land."""
    z_dir = os.path.join(CACHE, str(z))
    if not os.path.isdir(z_dir):
        return []
    n = 2 ** z
    broken = []
    for x_name in os.listdir(z_dir):
        x_dir = os.path.join(z_dir, x_name)
        if not os.path.isdir(x_dir):
            continue
        try:
            x = int(x_name)
        except ValueError:
            continue
        for fname in os.listdir(x_dir):
            if not fname.endswith('.webp'):
                continue
            path = os.path.join(x_dir, fname)
            if os.path.getsize(path) >= 100:
                continue
            y = int(fname.replace('.webp', ''))
            lon_c = (x + 0.5) / n * 360 - 180
            lat_c = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 0.5) / n))))
            if (math.floor(lat_c), math.floor(lon_c)) in land_cells:
                broken.append(path)
    return broken


if __name__ == '__main__':
    with multiprocessing.Pool(13) as pool:
        results = pool.map(scan_zoom, range(0, 13))

    all_broken = []
    for z, paths in enumerate(results):
        if paths:
            print(f'  z{z}: {len(paths)} broken tiles')
            all_broken.extend(paths)

    print(f'\nTotal: {len(all_broken)} broken tiles over land')

    if '--dry-run' in sys.argv:
        print('Dry run — not deleting')
        sys.exit(0)

    for p in all_broken:
        os.unlink(p)
    print(f'Deleted {len(all_broken)} tiles')

    marker = os.path.join(CACHE, 'COMPLETED_z12')
    if os.path.exists(marker):
        os.unlink(marker)
        print('Removed COMPLETED_z12 marker')
