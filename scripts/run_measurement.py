#!/usr/bin/env python3
"""Run one measurement script under enforced caps (S14d, owner decision 371).

    $PY -B scripts/run_measurement.py [flags] scripts/measure_x.py [-- script args]

Caps (flag overrides the script's MEASUREMENT_BUDGET, which overrides the default):
  wall clock   300 s    --wall S        hard maximum 900 s
  generated    1 GiB    --max-data-mib  hard maximum 4 GiB; counted under the output directory this runner creates
  memory       3 GiB    --max-rss-mib   hard maximum 6 GiB; summed over every process the child started
Only the caller's --approve-decision N (a decision number) goes over a hard maximum; a script's own declaration cannot.
The child gets MEASUREMENT_OUTPUT_DIR and TMPDIR pointing at the output directory, so temporary files count.
Also watched: the free space of the output directory's volume and of /tmp's (a drop over the data cap is a breach).
NOT covered: data written to another volume. Data written outside the output directory is detected, never deleted.
Descendants are found by walking parent pids (so setsid children are caught) and are killed when the child exits or on
a breach, by pid from that tree only. Peak memory is also checked after exit (getrusage), and the data is recounted.
On a breach the tree is killed, the data deleted, one plain line printed and the status is 3; runner errors exit 4.
The output directory is deleted at the end unless --keep.
Nothing here runs or imports the measured script: its budget is read from the source as a literal.
"""
import argparse
import ast
import math
import os
import resource
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
HARD_MAX_DATA_MIB = 4096
DEFAULT_RSS_MIB = 3072
HARD_MAX_RSS_MIB = 6144
HARD_MAX = {'wall_seconds': HARD_MAX_WALL_SECONDS, 'data_mib': HARD_MAX_DATA_MIB, 'rss_mib': HARD_MAX_RSS_MIB}
POLL_SECONDS = 0.2
WALK_SECONDS = 0.5        # a poll never waits longer than this for the directory walk
PS_SECONDS = 3            # nor for ps
VOLUME_SLACK_MIB = 16     # free-space noise from other writers on the same volume
# `decision` is accepted in a declaration but never honoured: only the caller's --approve-decision raises a hard maximum.
BUDGET_KEYS = {'wall_seconds', 'data_mib', 'rss_mib', 'decision'}
BREACH = 3
ERROR = 4
REFUSED = 2


class RunnerError(Exception):
    pass


def read_budget(path):
    """The literal MEASUREMENT_BUDGET dict of a script, read without importing it; None if absent."""
    tree = ast.parse(Path(path).read_text(), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'MEASUREMENT_BUDGET' for t in node.targets):
            return ast.literal_eval(node.value)
    return None


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def budget_errors(budget):
    """Plain problems with a declared budget; an empty list means it is acceptable.
    Declared values are positive integers and may not exceed the hard maxima: a script cannot approve itself."""
    if not isinstance(budget, dict):
        return ['MEASUREMENT_BUDGET must be a literal dict']
    errors = [f'unknown key {key!r}' for key in budget if key not in BUDGET_KEYS]
    for key, ceiling in HARD_MAX.items():
        if key not in budget:
            continue
        value = budget[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f'{key} must be a positive whole number')
        elif value > ceiling:
            errors.append(f'{key} {value} is over the hard maximum {ceiling}: only the caller can raise it, with --approve-decision N')
    return errors


def tree_bytes(path, max_seconds=None):
    """(bytes allocated under path, complete). Sparse files count what they use; the walk may stop early."""
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    total = 0
    for base, dirs, files in os.walk(path):
        if deadline is not None and time.monotonic() >= deadline:
            return total, False
        for name in files:
            if deadline is not None and time.monotonic() > deadline:
                return total, False
            try:
                info = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            total += min(info.st_size, info.st_blocks * 512) if hasattr(info, 'st_blocks') else info.st_size
    return total, True


def free_bytes(path):
    info = os.statvfs(path)
    return info.f_bavail * info.f_frsize


def process_table():
    """{pid: (ppid, pgid, rss_bytes)} for every process. A ps failure is an error, never zero."""
    try:
        done = subprocess.run(['ps', '-axo', 'pid=,ppid=,pgid=,rss='], capture_output=True, text=True, timeout=PS_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        raise RunnerError(f'could not read the process table with ps ({error})')
    if done.returncode != 0:
        raise RunnerError(f'ps failed with status {done.returncode}')
    table = {}
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 4 and all(part.isdigit() for part in parts):
            table[int(parts[0])] = (int(parts[1]), int(parts[2]), int(parts[3]) * 1024)
    if not table:
        raise RunnerError('ps returned no processes')
    return table


class Tree:
    """Every process the child started: ppid walk from it, its process group, and earlier finds that are still alive."""

    def __init__(self, root):
        self.root = root
        self.known = {root: root}   # pid -> pgid when found (guards against pid reuse)

    def refresh(self, table):
        members = {pid for pid, (_, pgid, _) in table.items() if pgid == self.root}
        children = {}
        for pid, (ppid, _, _) in table.items():
            children.setdefault(ppid, []).append(pid)
        stack = [self.root]
        while stack:
            pid = stack.pop()
            if pid in table:
                members.add(pid)
                stack.extend(children.get(pid, ()))
        for pid, pgid in list(self.known.items()):
            if pid in table and table[pid][1] == pgid:
                members.add(pid)
        for pid in members:
            self.known.setdefault(pid, table[pid][1])
        self.known = {pid: pgid for pid, pgid in self.known.items() if pid in table}
        return members

    def kill(self, process=None):
        """TERM then KILL, only for pids found in this tree and the group this runner started."""
        for sig, grace in ((signal.SIGTERM, 1.5), (signal.SIGKILL, 3.0)):
            try:
                members = self.refresh(process_table())
            except RunnerError:
                members = set(self.known)
            members.discard(os.getpid())
            for pid in members:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                os.killpg(self.root, sig)
            except (ProcessLookupError, PermissionError):
                pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if process is not None:
                    process.poll()
                try:
                    alive = self.refresh(process_table()) - {os.getpid()}
                except RunnerError:
                    alive = set()
                if not alive and (process is None or process.poll() is not None):
                    return
                time.sleep(0.05)
        if process is not None:
            process.wait()


def delete_tree(path):
    """Delete the data directory, including subdirectories the child made unreadable. True when it is gone."""
    for base, dirs, _ in os.walk(path):
        for name in dirs:
            try:
                os.chmod(os.path.join(base, name), 0o700)
            except OSError:
                pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def fail(message, code=REFUSED):
    print(f'measurement refused: {message}', file=sys.stderr)
    return code


def finite_number(text):
    value = float(text)
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError('must be a finite number')
    return value


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], usage='%(prog)s [flags] script [-- script args]')
    parser.add_argument('--wall', type=finite_number, help='wall-clock cap in seconds')
    parser.add_argument('--max-data-mib', type=finite_number, help='cap on generated data in MiB')
    parser.add_argument('--max-rss-mib', type=finite_number, help='cap on peak memory in MiB')
    parser.add_argument('--approve-decision', help='owner decision number that approves going over a hard maximum')
    parser.add_argument('--keep', action='store_true', help='keep the generated data and name its directory')
    parser.add_argument('script')
    parser.add_argument('script_args', nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def limits(args):
    """(wall, data_mib, rss_mib) or a refusal message."""
    script = Path(args.script)
    if not script.is_file():
        return f'{script} is not a file'
    try:
        budget = read_budget(script) or {}
    except (SyntaxError, ValueError, OSError) as error:
        return f'{script.name}: cannot read MEASUREMENT_BUDGET ({error})'
    approved = args.approve_decision
    if approved is not None and not (approved.isdigit() and int(approved) > 0):
        return '--approve-decision must be a decision number such as 371'
    errors = budget_errors(budget)
    if approved is not None:
        errors = [error for error in errors if 'hard maximum' not in error]
    if errors:
        return f'{script.name}: ' + '; '.join(errors)
    chosen = {
        'wall_seconds': args.wall if args.wall is not None else budget.get('wall_seconds', DEFAULT_WALL_SECONDS),
        'data_mib': args.max_data_mib if args.max_data_mib is not None else budget.get('data_mib', DEFAULT_DATA_MIB),
        'rss_mib': args.max_rss_mib if args.max_rss_mib is not None else budget.get('rss_mib', DEFAULT_RSS_MIB),
    }
    for name, value in chosen.items():
        if not _positive(value):
            return f'{name} must be a positive finite number'
    if approved is None:
        for name, ceiling in HARD_MAX.items():
            if chosen[name] > ceiling:
                return (f'{name} {chosen[name]:g} is over the hard maximum {ceiling}; an explicit owner approval is needed: '
                        'pass --approve-decision N naming the decision number')
    return chosen['wall_seconds'], chosen['data_mib'], chosen['rss_mib']


def maxrss_bytes():
    value = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return value if sys.platform == 'darwin' else value * 1024


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    chosen = limits(args)
    if isinstance(chosen, str):
        return fail(chosen)
    wall, data_mib, rss_mib = chosen
    script_args = args.script_args[1:] if args.script_args[:1] == ['--'] else args.script_args
    out_dir = None
    breach = problem = None
    peak_data = peak_rss = 0
    returncode = 0
    kept = False
    started = time.monotonic()
    try:
        out_dir = Path(tempfile.mkdtemp(prefix='measure-'))
        volumes = {}
        for probe in (out_dir, '/tmp'):
            try:
                volumes.setdefault(os.stat(probe).st_dev, (str(probe), free_bytes(probe)))
            except OSError:
                pass
        slack = (data_mib + VOLUME_SLACK_MIB) * 2**20

        def volume_drop():
            worst = 0
            for probe, before in volumes.values():
                try:
                    worst = max(worst, before - free_bytes(probe))
                except OSError:
                    pass
            return worst

        def verdict(elapsed):
            if elapsed > wall:
                return (f'wall clock limit of {wall:g} s exceeded after {elapsed:.0f} s',
                        'raise it with --wall S (over 900 needs --approve-decision N) or set wall_seconds in the script\'s MEASUREMENT_BUDGET')
            if peak_data > data_mib * 2**20:
                return (f'generated data limit of {data_mib:g} MiB exceeded ({peak_data / 2**20:.1f} MiB written)',
                        'measure a smaller size, or raise it with --max-data-mib N or data_mib in MEASUREMENT_BUDGET')
            drop = volume_drop()
            if drop > slack:
                return (f'disk use outside the data directory: free space fell by {drop / 2**20:.0f} MiB, over the {data_mib:g} MiB limit',
                        'write only under $MEASUREMENT_OUTPUT_DIR (or the TMPDIR the runner sets); data written elsewhere is not deleted by the runner')
            if peak_rss > rss_mib * 2**20:
                return (f'memory limit of {rss_mib:g} MiB exceeded ({peak_rss / 2**20:.0f} MiB in use)',
                        'measure a smaller size, or raise it with --max-rss-mib N or rss_mib in MEASUREMENT_BUDGET')
            return None

        env = dict(os.environ, MEASUREMENT_OUTPUT_DIR=str(out_dir), TMPDIR=str(out_dir))
        process = subprocess.Popen([sys.executable, '-B', args.script, *script_args], env=env, start_new_session=True)
        tree = Tree(process.pid)
        try:
            while True:
                elapsed = time.monotonic() - started
                breach = verdict(elapsed) if elapsed > wall else None
                if breach is None:
                    table = process_table()
                    members = tree.refresh(table)
                    peak_rss = max(peak_rss, sum(table[pid][2] for pid in members))
                    peak_data = max(peak_data, tree_bytes(out_dir, WALK_SECONDS)[0])
                    breach = verdict(time.monotonic() - started)
                if breach or process.poll() is not None:
                    break
                time.sleep(POLL_SECONDS)
        finally:
            tree.kill(process)
        returncode = process.returncode
        peak_rss = max(peak_rss, maxrss_bytes())
        peak_data = max(peak_data, tree_bytes(out_dir, 30)[0])
        breach = breach or verdict(time.monotonic() - started)
    except RunnerError as error:
        problem = str(error)
    except OSError as error:
        problem = f'could not start the measurement ({error})'
    finally:
        if out_dir is not None:
            kept = args.keep and breach is None and problem is None
            if not kept and not delete_tree(out_dir):
                problem = (problem + '; ' if problem else '') + f'could not delete the generated data in {out_dir}: remove it by hand'
    seconds = time.monotonic() - started
    if breach:
        if problem:
            print(f'measurement stopped: {breach[0]}; {problem}; {breach[1]}.')
            return ERROR
        print(f'measurement stopped: {breach[0]}; its generated data was deleted; {breach[1]}.')
        return BREACH
    if problem:
        print(f'measurement error: {problem}', file=sys.stderr)
        return ERROR
    note = f'; data kept in {out_dir}' if kept else '; generated data deleted'
    print(f'measurement finished (exit {returncode}): {seconds:.1f} s, {peak_data} bytes written, '
          f'peak memory {peak_rss / 2**20:.0f} MiB{note}')
    return returncode if returncode > 0 else (1 if returncode < 0 else 0)


if __name__ == '__main__':
    sys.exit(main())
