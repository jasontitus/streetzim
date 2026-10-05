"""Independent shared Wikidata selection benchmark; never reads production caches."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import resource
import signal
import statistics
import subprocess
import sys
import threading
import time

COUNT = 500_000
PREFIXES = 90
SELECTED = 30_000


def require_absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise FileExistsError(f'evidence path already exists: {path}')


def write_new(path, contents):
    # O_EXCL protects against a path created after the preflight, including
    # a symlink or hard link to a source snapshot or earlier evidence.
    with path.open('x') as output:
        output.write(contents)


def cgroup_limits(root=Path('/sys/fs/cgroup')):
    """Actual limits, keeping unavailable and unlimited distinct from zero."""
    limits = {'memory_bytes': None, 'swap_bytes': None, 'cpus': None,
              'cpu_max': None, 'errors': {}}
    for filename, field in [('memory.max', 'memory_bytes'),
                            ('memory.swap.max', 'swap_bytes')]:
        try:
            value = (root / filename).read_text().strip()
            limits[field] = 'max' if value == 'max' else int(value)
        except (OSError, ValueError) as exc:
            limits['errors'][filename] = str(exc)
    try:
        value = (root / 'cpu.max').read_text().strip()
        quota, period = value.split()
        period = int(period)
        if period <= 0:
            raise ValueError('CPU period must be positive')
        limits['cpu_max'] = value
        if quota != 'max':
            quota = int(quota)
            if quota <= 0:
                raise ValueError('CPU quota must be positive')
            limits['cpus'] = quota / period
    except (OSError, ValueError) as exc:
        limits['errors']['cpu.max'] = str(exc)
    return limits


def memory(pid):
    values = {}
    try:
        for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines():
            fields = line.split()
            if fields and fields[0] in ('Rss:', 'Pss:', 'Anonymous:'):
                values[fields[0][:-1].lower()] = int(fields[1]) * 1024
    except (FileNotFoundError, ProcessLookupError):
        return None
    return values


def prepare(args):
    sources = {n: hashlib.sha256((args.evidence / n).read_bytes()).hexdigest()
               for n in ['wikidata_baseline_29ec7da.py', 'wikidata_current_final.py']}
    require_absent(args.evidence / 'selected.json')
    require_absent(args.evidence / 'fixture.json')
    cache = args.cache
    cache.mkdir(parents=True, exist_ok=False)
    selected = []
    position = 0
    for bucket in range(PREFIXES):
        count = COUNT // PREFIXES + (bucket < COUNT % PREFIXES)
        entries = {}
        prefix = bucket + 10
        for local in range(count):
            qid = f'Q{prefix}{local:07d}'
            entry = {'qid': qid, 'label': f'Place {qid}',
                     'description': (f'A synthetic place with geographic facts for {qid}. ' * 3)[:120],
                     'instance_of': 'human settlement'}
            if position % 3 == 0:
                entry['country'] = f'Country {position % 11}'
            if position % 4 == 0:
                entry['population'] = position * 7 + 1
            if position % 10 == 0:
                entry['area_km2'] = position / 100 + 0.5
            if position % 10 < 3:
                entry['wikipedia_title'] = f'Place_{qid}'
                entry['wikipedia_url'] = f'https://en.wikipedia.org/wiki/Place_{qid}'
                if position % 10 < 2:
                    entry['extract'] = (f'Place {qid} is a synthetic benchmark location. ' * 12)[:500]
            if position % 50 < 3:
                selected.append(qid)
            entries[qid] = entry
            position += 1
        write_new(cache / f'{prefix}.json', json.dumps(entries, separators=(',', ':')))
    assert position == COUNT and len(selected) == SELECTED
    write_new(args.evidence / 'selected.json', json.dumps(selected))
    manifest = {'total_entries': COUNT, 'buckets': PREFIXES, 'updated': 'synthetic'}
    write_new(cache / 'manifest.json', json.dumps(manifest))
    facts = {
        'entries': COUNT, 'buckets': PREFIXES, 'selected': SELECTED, 'selected_fraction': 0.06,
        'json_bytes': sum(p.stat().st_size for p in cache.glob('*.json')),
        'fields': 'all: qid,label,description120,instance_of; country1/3; population1/4; area1/10; Wikipedia title+URL3/10; extract500 2/10',
        'bucket_selection': 'global index modulo50 in[0,1,2], spread over all90prefix buckets',
        'synthetic_limit': 'Representative cache field shape only; frequencies and text lengths are explicit synthetic parameters, not measured China distribution.',
        'sources': sources,
    }
    write_new(args.evidence / 'fixture.json', json.dumps(facts, indent=2))
    print(json.dumps(facts))


def worker(args):
    sys.path.insert(0, str(args.repo))
    filename = 'wikidata_baseline_29ec7da.py' if args.worker == 'baseline' else 'wikidata_current_final.py'
    spec = importlib.util.spec_from_file_location('wd_review', args.evidence / filename)
    wc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wc)
    selected = set(json.loads((args.evidence / 'selected.json').read_text()))
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    with contextlib.redirect_stdout(io.StringIO()):
        data = wc.load_cache_for_zim(args.cache) if args.worker == 'baseline' else wc.load_cache_for_zim(args.cache, qids=selected)
    load_wall = time.perf_counter() - wall_start
    load_cpu = time.process_time() - cpu_start
    load_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    post_load = memory(os.getpid())
    assert len(data) == (COUNT if args.worker == 'baseline' else SELECTED)
    subset = {qid: data[qid] for qid in selected}
    digest = hashlib.sha256(json.dumps(subset, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    time.sleep(0.15)  # gives the monitor a sample of the live result
    print(json.dumps({'version': args.worker, 'loaded': len(data), 'selected': len(subset),
                      'selected_compact_sha256': digest, 'load_wall_s': load_wall,
                      'load_cpu_s': load_cpu, 'load_peak_rss_bytes': load_peak,
                      'post_load': post_load, 'platform': platform.platform(), 'python': sys.version}))


def run(args, version, repeat):
    command = [sys.executable, __file__, '--repo', str(args.repo), '--evidence', str(args.evidence),
               '--cache', str(args.cache), '--results', str(args.results), '--worker', version]
    stop = threading.Event()
    samples, errors = [], []
    start = time.monotonic()
    prefix = f'{version}-{repeat}'
    logpath = args.results / f'{prefix}.log'
    proc = None
    monitor = None
    with logpath.open('x') as log:
        try:
            proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            def sample():
                while not stop.is_set():
                    try:
                        mem = memory(proc.pid)
                        if mem:
                            samples.append({'elapsed_s': time.monotonic() - start, **mem})
                    except OSError as exc:
                        errors.append(str(exc))
                    stop.wait(0.05)
            monitor = threading.Thread(target=sample)
            monitor.start()
            _, status, usage = os.wait4(proc.pid, 0)
            proc.returncode = os.waitstatus_to_exitcode(status)
        except BaseException:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if proc.returncode is None:
                    proc.wait()
            raise
        finally:
            stop.set()
            if monitor is not None:
                monitor.join()
    result = {'version': version, 'repeat': repeat, 'command': command,
              'exit_code': proc.returncode, 'process_wall_s': time.monotonic() - start,
              'process_cpu_s': usage.ru_utime + usage.ru_stime,
              'wait4_peak_rss_bytes': usage.ru_maxrss * 1024,
              'sample_interval_s': 0.05, 'samples': samples, 'sampling_errors': errors,
              'peak_sampled': {key: max((s[key] for s in samples), default=0) for key in ['rss', 'pss', 'anonymous']}}
    if proc.returncode == 0:
        result['worker'] = json.loads(logpath.read_text().splitlines()[-1])
    write_new(args.results / f'{prefix}.json', json.dumps(result, indent=2))
    assert proc.returncode == 0, logpath.read_text()
    return result


def main(args):
    args.results.mkdir(parents=True, exist_ok=True)
    if any(args.results.iterdir()):
        raise FileExistsError(f'results directory must be empty: {args.results}')
    rows = []
    for repeat in range(3):
        for version in ['baseline', 'current']:
            rows.append(run(args, version, repeat))
    hashes = {r['worker']['selected_compact_sha256'] for r in rows}
    assert len(hashes) == 1
    summary = {'fixture': {k: v for k, v in json.loads((args.evidence / 'fixture.json').read_text()).items() if k != 'sources'},
               'selected_compact_sha256': next(iter(hashes)), 'runs': len(rows),
               'limits': cgroup_limits(),
               'provenance': {'baseline_git': '29ec7da',
                              'sources': {name: hashlib.sha256((args.evidence / name).read_bytes()).hexdigest() for name in ['wikidata_baseline_29ec7da.py', 'wikidata_current_final.py']},
                              'operator_image_label': args.image_label,
                              'image_label_verified_by_helper': False,
                              'measurement': 'Single fresh child per run; /proc/PID/smaps_rollup samplesRSS/PSS/anonymous at0.05s; exact wait4 peakRSS; worker wall/CPU excludes startup and hash validation.'}}
    for version in ['baseline', 'current']:
        group = [r for r in rows if r['version'] == version]
        summary[version] = {
            'median_load_wall_s': statistics.median(r['worker']['load_wall_s'] for r in group),
            'median_load_cpu_s': statistics.median(r['worker']['load_cpu_s'] for r in group),
            'median_load_peak_rss_bytes': statistics.median(r['worker']['load_peak_rss_bytes'] for r in group),
            'median_wait4_peak_rss_bytes': statistics.median(r['wait4_peak_rss_bytes'] for r in group),
            'median_sampled_peak_rss_bytes': statistics.median(r['peak_sampled']['rss'] for r in group),
            'median_sampled_peak_pss_bytes': statistics.median(r['peak_sampled']['pss'] for r in group),
            'median_sampled_peak_anonymous_bytes': statistics.median(r['peak_sampled']['anonymous'] for r in group),
        }
    write_new(args.results / 'summary.json', json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path('/review-repo'))
    parser.add_argument('--evidence', type=Path, default=Path('/evidence'))
    parser.add_argument('--cache', type=Path, default=Path('/evidence/cache'))
    parser.add_argument('--results', type=Path, default=Path('/results'))
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--worker', choices=['baseline', 'current'])
    parser.add_argument('--image-label', help='Optional operator-supplied image identity; not verified by this helper')
    args = parser.parse_args()
    if sys.platform != 'linux':
        parser.error('benchmark requires Linux /proc and Linux wait4 RSS units')
    if args.prepare:
        prepare(args)
    elif args.worker:
        worker(args)
    else:
        main(args)


if __name__ == '__main__':
    cli()
