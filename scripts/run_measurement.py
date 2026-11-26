#!/usr/bin/env python3
"""Run one measurement script under enforced caps (S14d, owner decision 371).

    $PY -B scripts/run_measurement.py [flags] scripts/measure_x.py [-- script args]

Caps (flag overrides the script's MEASUREMENT_BUDGET, which overrides the default):
  wall clock   300 s    --wall S        hard maximum 900 s unless --approve-decision N names a decision
  generated    1 GiB    --max-data-mib  everything under the output directory this runner creates
  memory       3 GiB    --max-rss-mib   peak resident memory summed over the child's process group
The child gets MEASUREMENT_OUTPUT_DIR and TMPDIR pointing at the output directory, so temporary files
count. On a breach only the process group this runner started is killed, the data is deleted, one plain
line is printed and the exit status is 3. The output directory is deleted at the end unless --keep.
Nothing here runs or imports the measured script: its budget is read from the source as a literal.
"""
import argparse
import ast
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

DEFAULT_WALL_SECONDS = 300
HARD_MAX_WALL_SECONDS = 900
DEFAULT_DATA_MIB = 1024
DEFAULT_RSS_MIB = 3072
POLL_SECONDS = 0.2
BUDGET_KEYS = {'wall_seconds', 'data_mib', 'rss_mib', 'decision'}
BREACH = 3
REFUSED = 2


def read_budget(path):
    """The literal MEASUREMENT_BUDGET dict of a script, read without importing it; None if absent."""
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'MEASUREMENT_BUDGET' for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def budget_errors(budget):
    """Plain problems with a declared budget; an empty list means it is acceptable."""
    if not isinstance(budget, dict):
        return ['MEASUREMENT_BUDGET must be a literal dict']
    errors = [f'unknown key {key!r}' for key in budget if key not in BUDGET_KEYS]
    for key in ('wall_seconds', 'data_mib', 'rss_mib'):
        if key in budget and not _number(budget[key]):
            errors.append(f'{key} must be a positive number')
    decision = budget.get('decision')
    if 'decision' in budget and (not isinstance(decision, int) or isinstance(decision, bool) or decision <= 0):
        errors.append('decision must be a positive decision number')
    wall = budget.get('wall_seconds')
    if _number(wall) and wall > HARD_MAX_WALL_SECONDS and not (isinstance(decision, int) and not isinstance(decision, bool) and decision > 0):
        errors.append(f'wall_seconds {wall} is over the {HARD_MAX_WALL_SECONDS} s hard maximum: declare the owner decision number as "decision"')
    return errors


def tree_bytes(path):
    """Bytes allocated under path (sparse files count what they use)."""
    total = 0
    for base, dirs, files in os.walk(path):
        for name in files:
            try:
                info = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            total += min(info.st_size, info.st_blocks * 512) if hasattr(info, 'st_blocks') else info.st_size
    return total


def group_rss_bytes(pgid):
    """Resident memory summed over every live process in the process group."""
    try:
        out = subprocess.run(['ps', '-axo', 'pgid=,rss='], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == str(pgid) and parts[1].isdigit():
            total += int(parts[1]) * 1024
    return total


def kill_group(pgid, process):
    for sig, grace in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(pgid, signal.SIGKILL)  # stragglers of the same group only
    except ProcessLookupError:
        pass
    process.wait()


def fail(message):
    print(f'measurement refused: {message}', file=sys.stderr)
    return REFUSED


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], usage='%(prog)s [flags] script [-- script args]')
    parser.add_argument('--wall', type=float, help='wall-clock cap in seconds')
    parser.add_argument('--max-data-mib', type=float, help='cap on generated data in MiB')
    parser.add_argument('--max-rss-mib', type=float, help='cap on peak memory in MiB')
    parser.add_argument('--approve-decision', help='owner decision number that approves a wall clock over 900 s')
    parser.add_argument('--keep', action='store_true', help='keep the generated data and name its directory')
    parser.add_argument('script')
    parser.add_argument('script_args', nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def limits(args):
    """(wall, data_mib, rss_mib) or a refusal message."""
    script = Path(args.script)
    if not script.is_file():
        return f'{script} is not a file'
    budget = read_budget(script) or {}
    errors = budget_errors(budget)
    if errors:
        return f'{script.name}: ' + '; '.join(errors)
    wall = args.wall if args.wall is not None else budget.get('wall_seconds', DEFAULT_WALL_SECONDS)
    data = args.max_data_mib if args.max_data_mib is not None else budget.get('data_mib', DEFAULT_DATA_MIB)
    rss = args.max_rss_mib if args.max_rss_mib is not None else budget.get('rss_mib', DEFAULT_RSS_MIB)
    for name, value in (('--wall', wall), ('--max-data-mib', data), ('--max-rss-mib', rss)):
        if not _number(value):
            return f'{name} must be a positive number'
    approved = args.approve_decision
    if approved is not None and not (approved.isdigit() and int(approved) > 0):
        return '--approve-decision must be a decision number such as 371'
    if wall > HARD_MAX_WALL_SECONDS and approved is None and 'decision' not in budget:
        return (f'a wall clock of {wall:g} s is over the {HARD_MAX_WALL_SECONDS} s hard maximum; '
                'an explicit owner approval is needed: pass --approve-decision N naming the decision number')
    return wall, data, rss


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    chosen = limits(args)
    if isinstance(chosen, str):
        return fail(chosen)
    wall, data_mib, rss_mib = chosen
    script_args = args.script_args[1:] if args.script_args[:1] == ['--'] else args.script_args
    out_dir = Path(tempfile.mkdtemp(prefix='measure-'))
    env = dict(os.environ, MEASUREMENT_OUTPUT_DIR=str(out_dir), TMPDIR=str(out_dir))
    started = time.monotonic()
    process = subprocess.Popen([sys.executable, '-B', args.script, *script_args], env=env, start_new_session=True)
    pgid = process.pid
    peak_data = peak_rss = 0
    breach = None
    try:
        while process.poll() is None:
            elapsed = time.monotonic() - started
            peak_data = max(peak_data, tree_bytes(out_dir))
            peak_rss = max(peak_rss, group_rss_bytes(pgid))
            if elapsed > wall:
                breach = (f'wall clock limit of {wall:g} s exceeded after {elapsed:.0f} s',
                          'raise it with --wall S (over 900 needs --approve-decision N) or declare wall_seconds in the script\'s MEASUREMENT_BUDGET')
            elif peak_data > data_mib * 2**20:
                breach = (f'generated data limit of {data_mib:g} MiB exceeded ({peak_data / 2**20:.1f} MiB written)',
                          'measure a smaller size, or raise it with --max-data-mib N or data_mib in MEASUREMENT_BUDGET')
            elif peak_rss > rss_mib * 2**20:
                breach = (f'memory limit of {rss_mib:g} MiB exceeded ({peak_rss / 2**20:.0f} MiB in use)',
                          'measure a smaller size, or raise it with --max-rss-mib N or rss_mib in MEASUREMENT_BUDGET')
            if breach:
                kill_group(pgid, process)
                break
            time.sleep(POLL_SECONDS)
    except BaseException:
        kill_group(pgid, process)
        raise
    finally:
        peak_data = max(peak_data, tree_bytes(out_dir))
        kept = args.keep and breach is None
        if not kept:
            shutil.rmtree(out_dir, ignore_errors=True)
    seconds = time.monotonic() - started
    if breach:
        print(f'measurement stopped: {breach[0]}; its generated data was deleted; {breach[1]}.')
        return BREACH
    note = f'; data kept in {out_dir}' if kept else '; generated data deleted'
    print(f'measurement finished (exit {process.returncode}): {seconds:.1f} s, {peak_data} bytes written, '
          f'peak memory {peak_rss / 2**20:.0f} MiB{note}')
    return process.returncode if process.returncode > 0 else (1 if process.returncode < 0 else 0)


if __name__ == '__main__':
    sys.exit(main())
