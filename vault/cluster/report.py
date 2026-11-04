"""Summarise benchmark runs into Markdown tables.

    report.py results/<time>/runs.jsonl [--vmstat-dir results/<time>]

Prints throughput per configuration, batch commit times, placement rates,
fold times and, when vmstat logs are given, CPU use per VM during each
settlement window.
"""
import argparse
import datetime as dt
import json
from pathlib import Path
from statistics import mean, median


def parse(text):
    return dt.datetime.fromisoformat(text.replace('Z', '+00:00'))


def percentile(values, p):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * p
    low, high = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (k - low)


def load_vmstat(path):
    """(time, busy percent) samples. vmstat -t prints the timestamp in the last two columns."""
    samples = []
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) < 17 or not parts[0].isdigit():
            continue
        try:
            idle = int(parts[14])
            stamp = dt.datetime.fromisoformat(f'{parts[-2]} {parts[-1]}').replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        samples.append((stamp, 100 - idle))
    return samples


def busy_between(samples, start, end):
    window = [b for t, b in samples if start <= t <= end]
    return (round(mean(window)), max(window)) if window else (None, None)


def config_name(run):
    return 'batched' if run['mode'] == 'batched' else f"sharded K={run['shards']}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs')
    parser.add_argument('--vmstat-dir')
    args = parser.parse_args()
    runs = [json.loads(line) for line in Path(args.runs).read_text().splitlines() if line.strip()]
    vms = {}
    if args.vmstat_dir:
        for path in sorted(Path(args.vmstat_dir).glob('vmstat-*.log')):
            vms[path.stem.removeprefix('vmstat-')] = load_vmstat(path)

    order = sorted({config_name(r) for r in runs}, key=lambda n: (n != 'batched', int(n.split('=')[-1]) if '=' in n else 0))
    base = mean(r['orders_per_second'] for r in runs if r['mode'] == 'batched') if any(
        r['mode'] == 'batched' for r in runs) else None

    print('| Configuration | Runs | Orders/s mean | min | max | vs batched | Batches per run |')
    print('|---|---:|---:|---:|---:|---:|---:|')
    for name in order:
        group = [r for r in runs if config_name(r) == name]
        rates = [r['orders_per_second'] for r in group]
        speedup = f'{mean(rates) / base:.2f}x' if base else '-'
        print(f"| {name} | {len(group)} | {mean(rates):.1f} | {min(rates):.1f} | {max(rates):.1f} | {speedup} | "
              f"{mean(r['batches'] for r in group):.0f} |")

    print('\n| Configuration | Batch commit median s | p95 s | max s | Placement orders/s | Fold s |')
    print('|---|---:|---:|---:|---:|---:|')
    for name in order:
        group = [r for r in runs if config_name(r) == name]
        seconds = [s for r in group for s in r['batch_seconds']]
        placement = mean(r['placed'] / r['placement_seconds'] for r in group)
        folds = [r['fold_seconds'] for r in group if 'fold_seconds' in r]
        fold = f'{mean(folds):.2f}' if folds else '-'
        print(f'| {name} | {median(seconds):.2f} | {percentile(seconds, 0.95):.2f} | {max(seconds):.2f} | '
              f'{placement:.1f} | {fold} |')

    if vms:
        names = sorted(vms)
        print('\nCPU busy during settlement, mean and peak percent per VM:\n')
        print('| Run | ' + ' | '.join(names) + ' |')
        print('|---|' + '---:|' * len(names))
        for r in runs:
            start, end = parse(r['settle_started']), parse(r['settle_ended'])
            cells = []
            for vm in names:
                avg, peak = busy_between(vms[vm], start, end)
                cells.append('-' if avg is None else f'{avg} / {peak}')
            print(f"| {r['label']} | " + ' | '.join(cells) + ' |')

    print('\nAll runs balanced:', all(r['balance']['ok'] for r in runs))


if __name__ == '__main__':
    main()
