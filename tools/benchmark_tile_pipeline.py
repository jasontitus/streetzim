#!/usr/bin/env python3
"""Reproducible setup/scheduling microbenchmarks, without network or large inputs.

Run with the builder's Python environment. Baseline modules are read from a
Git ref or --baseline-root; every sample gets a fresh subprocess. Tile bounds
use an empty indexed MBTiles to isolate setup cost. Satellite coordinates are
all synthetic cache hits to isolate coordinate/Future scheduling overhead.
These results do not measure image encoding or end-to-end ZIM throughput.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import platform
import resource
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent


def sample(source: Path, scenario: str, work: Path, satellite_zoom: int) -> dict:
    sys.path.insert(0, str(ROOT))
    filename = 'satellite.py' if scenario == 'satellite-cached' else 'tiles.py'
    spec = importlib.util.spec_from_file_location('benchmarked_tiles', source / 'streetzim' / filename)
    if spec is None or spec.loader is None:
        raise ValueError(f'cannot load {source / filename}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if scenario == 'satellite-cached':
        from PIL import Image  # noqa: F401 -- import before timing / patching paths
        module.CACHE_DIR = str(work / 'cache')
        sources, _ = module.satellite_cache_dirs()
        Path(sources).mkdir(parents=True, exist_ok=True)
        dest = work / 'satellite'
        dest.mkdir()
        exists, getsize = module.os.path.exists, module.os.path.getsize
        module.os.path.exists = lambda p: str(p).endswith('.webp') or exists(p)
        module.os.path.getsize = lambda p: 1 if str(p).endswith('.webp') else getsize(p)
        module.os.cpu_count = lambda: 1  # four workers, identical in both versions
        with contextlib.redirect_stdout(io.StringIO()):
            started = time.perf_counter()
            count = module.download_satellite_tiles('-125,25,-66,49', str(dest),
                        max_zoom=satellite_zoom, sat_format='webp', tile_size=256)
            elapsed = time.perf_counter() - started
        details = {'max_zoom': satellite_zoom, 'synthetic_cached_tiles': count, 'workers': 4}
    else:
        path = work / 'empty.mbtiles'
        with sqlite3.connect(path) as conn:
            conn.executescript('CREATE TABLE tiles (zoom_level INT, tile_column INT, '
                               'tile_row INT, tile_data BLOB); CREATE INDEX tile_index '
                               'ON tiles (zoom_level, tile_column, tile_row);')
        bbox = (-125, 25, -66, 49) if scenario == 'tile-us' else (-124.5, 32, -114, 42)
        started = time.perf_counter()
        count = sum(1 for _ in module.iter_tiles_from_mbtiles(path, bbox=bbox, zoom_level=14))
        elapsed = time.perf_counter() - started
        details = {'bbox': bbox, 'zoom': 14, 'yielded_tiles': count}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() != 'Darwin':
        rss *= 1024  # Darwin reports bytes; Linux reports KiB.
    return {'scenario': scenario, 'elapsed_s': elapsed, 'peak_rss_bytes': rss, **details}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline-ref', default='d5c32b6')
    p.add_argument('--baseline-root', type=Path)
    p.add_argument('--repetitions', type=int, default=3)
    p.add_argument('--satellite-zoom', type=int, default=11)
    p.add_argument('--output', type=Path)
    p.add_argument('--child-source', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--child-scenario', help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.repetitions < 1 or not 0 <= args.satellite_zoom <= 14:
        p.error('need positive --repetitions and --satellite-zoom in 0..14')
    with tempfile.TemporaryDirectory(prefix='streetzim-tile-benchmark-') as folder:
        work = Path(folder)
        if args.child_source:
            print(json.dumps(sample(args.child_source, args.child_scenario, work, args.satellite_zoom)))
            return
        baseline = args.baseline_root or work / 'baseline'
        if args.baseline_root is None:
            (baseline / 'streetzim').mkdir(parents=True)
            for module in ('tiles.py', 'satellite.py'):
                data = subprocess.check_output(['git', 'show', f'{args.baseline_ref}:streetzim/{module}'], cwd=ROOT)
                (baseline / 'streetzim' / module).write_bytes(data)
        samples = []
        for scenario in ('tile-us', 'tile-california', 'satellite-cached'):
            for repetition in range(args.repetitions):
                for variant, source in (('baseline', baseline), ('current', ROOT)):
                    result = subprocess.check_output([sys.executable, str(Path(__file__).resolve()),
                        '--child-source', str(source.resolve()), '--child-scenario', scenario,
                        '--satellite-zoom', str(args.satellite_zoom)], cwd=ROOT, text=True)
                    samples.append({'variant': variant, 'repetition': repetition, **json.loads(result)})
        medians = []
        for scenario in ('tile-us', 'tile-california', 'satellite-cached'):
            for variant in ('baseline', 'current'):
                rows = [s for s in samples if s['scenario'] == scenario and s['variant'] == variant]
                medians.append({'scenario': scenario, 'variant': variant,
                    'elapsed_s': statistics.median(s['elapsed_s'] for s in rows),
                    'peak_rss_bytes': statistics.median(s['peak_rss_bytes'] for s in rows)})
        report = {'benchmark': 'tile-pipeline', 'baseline': str(args.baseline_root or args.baseline_ref),
                  'platform': platform.platform(), 'python': sys.version, 'samples': samples,
                  'medians': medians, 'scope': 'bounds setup and synthetic cache-hit scheduling only'}
        result = json.dumps(report, indent=2) + '\n'
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(result)
        else:
            print(result, end='')


if __name__ == '__main__':
    main()
