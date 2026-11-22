"""Independent fixtures/oracles for the ML red tests; no production implementation.

The seal verifier is a test GA-19 adapter: canonical events + manifest are HMAC
bound in a receipt OUTSIDE the project. It deliberately does not implement the
real GA-19 integration. All git commands are fixture/oracle commands in temp
repositories. No socket, network, user's HOME, or real project is used.

Trace hook semantics are specified by merge_library.IOCall. The kill oracle
uses actual SIGKILL, before AND after every observed filesystem/SQLite call,
including recovery calls. Fault runs inject once at each eligible call, then
retry without the fault. Fixed expected tree maps are chosen from durable
history, never from staging. Hardware power-loss evidence is separate.
"""
from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from codex_agents import merge_library as ml

GIT_ENV = {
    "PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "ML fixture", "GIT_AUTHOR_EMAIL": "ml@example.invalid",
    "GIT_COMMITTER_NAME": "ML fixture", "GIT_COMMITTER_EMAIL": "ml@example.invalid",
}
MUTATIONS = {"exchange", "rename_noreplace", "mkdir", "rmdir", "unlink"}
FAULTABLE = {"write", "fsync", "fullfsync", "exchange", "rename_noreplace", "link",
             "unlink", "mkdir", "rmdir", "open", "ftruncate", "sqlite_write", "sqlite_commit"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(root, *args, data=None):
    return subprocess.run(["git", "-C", str(root), *args], input=data, env=GIT_ENV,
                          capture_output=True, check=True, timeout=8).stdout


def content(data, mode=0o644):
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    return ml.Content(data, mode, blob)


def event_value(event):
    def version(item):
        return None if item is None else {"sha256": digest(item.data), "mode_bits": item.mode_bits,
                                         "git_blob": item.git_blob}
    return {"v": event.v, "kind": event.kind, "path": event.path,
            "base": version(event.base), "new": version(event.new)}


def binding(batch):
    return canonical({"base": batch.base_commit, "child": batch.child_id,
                      "events": [event_value(e) for e in batch.events],
                      "manifest": batch.manifest.hex(), "candidate_digest": batch.candidate_digest,
                      "git_link": batch.git_link, "batch_digest": batch.batch_digest})


def put(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def tree(root, *, metadata=False, control=False):
    """No-follow independent map. FIFOs/devices are NEVER opened by the oracle."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        if not control and Path(directory) == root:
            dirs[:] = [d for d in dirs if d not in (".git", ".cocolo")]
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            key = path.relative_to(root).as_posix()
            mode = stat.S_IMODE(info.st_mode)
            data = path.read_bytes() if stat.S_ISREG(info.st_mode) else (
                os.readlink(path) if stat.S_ISLNK(info.st_mode) else None)
            value = (stat.S_IFMT(info.st_mode), mode, data)
            if metadata:
                value += (info.st_ino, info.st_nlink,
                          None if stat.S_ISDIR(info.st_mode) else info.st_mtime_ns)
            result[key] = value
    return result


class Fixture:
    def __init__(self, parent=None):
        self.temp = tempfile.TemporaryDirectory(dir=str(parent or TEMP_ROOT))
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.private = self.root / "private"
        self.outside = self.root / "outside"
        for path in (self.project, self.private, self.outside):
            path.mkdir()
        self.secret = b"test-only-owner-seal-key"
        self.receipts = {}
        self.check_calls = []
        self.builder_calls = []
        git(self.project, "init", "-q")
        for name, data, mode in (("a.txt", b"one\ntwo\nthree\n", 0o644),
                                 ("z.txt", b"old z\n", 0o644),
                                 ("removed/old.txt", b"deleted\n", 0o644),
                                 ("tool", b"#!/bin/sh\necho old\n", 0o755)):
            put(self.project / name, data, mode)
        git(self.project, "add", "-A")
        git(self.project, "commit", "-qm", "base")
        self.base = git(self.project, "rev-parse", "HEAD").decode().strip()
        self.before = tree(self.project)
        self.policy = ml.Policy(self.verify, self.checks)

    def close(self):
        self.temp.cleanup()

    def checks(self, project):
        self.check_calls.append(tree(project))
        return True

    def verify(self, batch):
        signature = hmac.new(self.secret, binding(batch), hashlib.sha256).hexdigest()
        # Receipt location is private, not the child's editable directory.
        receipts = json.loads((self.private / "seals.json").read_text())
        return (hmac.compare_digest(signature, batch.approval)
                and receipts.get(batch.child_id) == signature)

    def batch(self, changes=None, child="child-1", *, modes=None):
        if changes is None:
            changes = {"a.txt": b"ONE\ntwo\nthree\n", "new/deep/add.txt": b"added\n",
                       "removed/old.txt": None, "z.txt": b"new z\n"}
        candidate = self.root / ("candidate-" + child)
        candidate.mkdir(exist_ok=True)
        events = []
        folders = set()
        for name, data in sorted(changes.items()):
            path = self.project / name
            old = content(path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.is_file() else None
            mode = (modes or {}).get(name, old.mode_bits if old else 0o644)
            new = content(data, mode) if data is not None else None
            if data is not None:
                put(candidate / name, data, mode)
                parent = Path(name).parent
                while parent != Path(".") and not (self.project / parent).exists():
                    folders.add(parent.as_posix())
                    parent = parent.parent
            events.append(ml.Event(name, old, new))
        events = [ml.Event(p, None, None, kind="folder") for p in sorted(folders)] + events
        manifest = canonical([event_value(e) for e in events])
        candidate_digest = digest(manifest)
        git_link = digest(b"fake GA-19 receipt\0" + self.base.encode() + manifest)
        batch_digest = digest(canonical({"events": [digest(canonical(event_value(e))) for e in events],
                                         "base": self.base, "candidate": candidate_digest}))
        batch = ml.SealedBatch(child, self.base, tuple(events), manifest, candidate_digest,
                               git_link, batch_digest, "", candidate)
        batch = replace(batch, approval=hmac.new(self.secret, binding(batch), hashlib.sha256).hexdigest())
        self.receipts[child] = batch.approval
        (self.private / "seals.json").write_bytes(canonical(self.receipts))
        return batch

    def land(self, batch=None, *, hook=None, policy=None):
        return ml.land(self.project, self.private, batch or self.batch(),
                       policy=policy or self.policy, hook=hook)

    def expected(self, batch, *, cleanup=True):
        expected = dict(self.before)
        for event in batch.events:
            if event.kind == "folder":
                expected[event.path] = (stat.S_IFDIR, 0o755, None)
            elif event.new is None:
                expected.pop(event.path, None)
            else:
                expected[event.path] = (stat.S_IFREG, event.new.mode_bits, event.new.data)
        if cleanup:
            emptied = {str(Path(e.path).parent) for e in batch.events
                       if e.kind == "file" and e.new is None}
            for directory in sorted(emptied, key=len, reverse=True):
                if directory != "." and not any(k.startswith(directory + "/") for k in expected):
                    expected.pop(directory, None)
        return expected


class Trace:
    def __init__(self, inject=None):
        self.calls = []
        self.inject = inject

    def __call__(self, call):
        self.calls.append(call)
        if self.inject:
            return self.inject(len(self.calls) - 1, call)
        return None


def checkpoint(name, action):
    fired = []

    def hook(call):
        if (not fired and call.operation == "checkpoint" and call.when == "before"
                and call.detail.get("name") == name):
            fired.append(call)
            action(call)
    return hook, fired


def child_run(action, timeout=8):
    """No pipes/sockets. Propagate non-kill child exceptions by a temp error file."""
    with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
        error_path = Path(directory) / "error"
        pid = os.fork()
        if pid == 0:
            try:
                action()
            except BaseException:
                import traceback
                exception_type = sys.exc_info()[0]
                (Path(directory) / "exception-type").write_text(exception_type.__name__)
                error_path.write_text(traceback.format_exc())
                os._exit(73)
            os._exit(0)
        deadline = time.monotonic() + timeout
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                if error_path.exists():
                    if (Path(directory) / "exception-type").read_text() == "NotImplementedError":
                        raise NotImplementedError(error_path.read_text())
                    raise AssertionError(error_path.read_text())
                return status
            if time.monotonic() >= deadline:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
                raise AssertionError("library call exceeded timeout (possible FIFO blocking)")
            # No socket or sleep: yield the CPU using a zero-time wait on child exit.
            os.sched_yield()


def fresh_queue(f):
    """A new interpreter cannot inherit a module-global queue from the sealing process."""
    code = (
        "import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "from codex_agents import merge_library as ml; "
        "print(json.dumps([q.child_id for q in ml.queue(Path(sys.argv[2]),Path(sys.argv[3]))]))"
    )
    result = subprocess.run([sys.executable, "-B", "-c", code, str(ORCHESTRATION_ROOT),
                             str(f.project), str(f.private)], cwd=f.root, env=GIT_ENV,
                            capture_output=True, timeout=8, check=True)
    return json.loads(result.stdout)


def report_oracle(test, fixture, report, batch):
    """ML-14: derive classes from disk independently, never from reported state."""
    events = {e.path: e for e in batch.events if e.kind == "file"}
    listed = {p.path: p for p in report.paths}
    test.assertTrue(set(events).issubset(listed))
    for name, event in events.items():
        path = fixture.project / name
        try:
            info = path.lstat()
            observed = (path.read_bytes(), stat.S_IMODE(info.st_mode)) if stat.S_ISREG(info.st_mode) else "owner"
        except FileNotFoundError:
            observed = None
        except (PermissionError, OSError):
            observed = "unknown"
        base = None if event.base is None else (event.base.data, event.base.mode_bits)
        new = None if event.new is None else (event.new.data, event.new.mode_bits)
        kind = ("unknown" if observed == "unknown" else "absent" if observed is None
                else "base" if observed == base else "new" if observed == new else "owner")
        test.assertEqual(kind, listed[name].classification, name)
        for retained in listed[name].retained:
            test.assertTrue(Path(retained).exists(), retained)
    staging = fixture.project / ".cocolo/staging" / batch.child_id
    if staging.exists():
        actual = {str(p) for p in staging.rglob("*") if p.is_file() and p.name != ".gitignore"}
        named = set(report.leftovers) | {p for row in report.paths for p in row.retained}
        test.assertTrue(actual.issubset(named), actual - named)


def assert_trace(test, calls):
    before = [(i, c) for i, c in enumerate(calls) if c.when == "before"]
    commits = [(i, c) for i, c in before if c.operation == "sqlite_commit"
               and c.detail.get("commit") != "queue"]
    test.assertEqual(["A", "B"], [c.detail["commit"] for _, c in commits])
    a, b = [i for i, _ in commits]
    mutations = [i for i, c in before if c.detail.get("scope") == "tree" and c.operation in MUTATIONS
                 and c.phase != "cleanup"]
    test.assertTrue(mutations)
    test.assertLess(a, min(mutations))
    test.assertLess(max(mutations), b)
    checked = [i for i, c in enumerate(calls) if c.operation == "project_checks" and c.when == "after"
               and c.result is True]
    test.assertTrue(checked)
    test.assertLess(max(checked), b)
    for index, call in commits:
        previous = calls[:index]
        test.assertTrue(any(c.operation in ("fullfsync", "fsync") and c.when == "after"
                            and c.detail.get("scope") == "history" for c in previous))
        test.assertTrue(call.detail.get("full_flush"))
        for parent in call.detail.get("parents", ()):
            test.assertTrue(any(c.path == parent and c.operation in ("fsync", "fullfsync")
                                and c.when == "after" for c in previous), parent)
            last_change = max((n for n, c in enumerate(previous)
                               if c.when == "after" and c.operation in MUTATIONS
                               and c.phase != "cleanup"
                               and any(p and str(Path(p).parent) == parent for p in (c.path, c.other))), default=-1)
            test.assertTrue(any(n > last_change and c.path == parent and c.when == "after"
                                and c.operation in ("fsync", "fullfsync")
                                for n, c in enumerate(previous)), parent)


def seed_applied(f, batch, *, state="applying"):
    """Internal crash fixture: complete tree and displaced originals, direction only in history."""
    staging = f.project / ".cocolo/staging" / batch.child_id
    staging.mkdir(parents=True, exist_ok=True)
    put(staging / ".gitignore", b"*\n")
    folders, paths, emptied = {}, {}, {}
    deleted = {e.path for e in batch.events if e.kind == "file" and e.new is None}
    for name in deleted:
        parent = (f.project / name).parent
        if parent != f.project and parent.is_dir():
            files = {p.relative_to(f.project).as_posix() for p in parent.rglob("*") if p.is_file()}
            if files and files.issubset(deleted):
                info = parent.stat()
                emptied[parent.relative_to(f.project).as_posix()] = (info.st_dev, info.st_ino)
    for index, event in enumerate(batch.events):
        path = f.project / event.path
        if event.kind == "folder":
            path.mkdir(parents=True, exist_ok=True)
            info = path.stat()
            folders[event.path] = (info.st_dev, info.st_ino)
            continue
        displaced = staging / f"e{index:05d}"
        paths[event.path] = str(displaced)
        if event.base:
            put(displaced, event.base.data, event.base.mode_bits)
        if event.new:
            put(path, event.new.data, event.new.mode_bits)
        elif path.exists():
            path.unlink()
    ml._seed_history(f.project, f.private, (ml.HistoryRow(1, batch, state),),
                     created_folders=folders, emptied_folders=emptied, staged_paths=paths)
    return staging, paths


def kill_oracle(test, operation, *, select=lambda c: c.operation != "checkpoint", oracle=None):
    """Discover, then SIGKILL at every selected before/after call in a fresh fixture."""
    with_fixture = Fixture()
    try:
        batch = with_fixture.batch()
        trace = Trace()
        operation(with_fixture, batch, trace)
        boundaries = [i for i, c in enumerate(trace.calls) if select(c)]
        test.assertTrue(boundaries, "a syscall hook must expose at least one boundary")
    finally:
        with_fixture.close()
    for boundary in boundaries:
        f = Fixture()
        try:
            batch = f.batch()
            def inject(index, call):
                if index == boundary:
                    os.kill(os.getpid(), signal.SIGKILL)
            status = child_run(lambda: operation(f, batch, Trace(inject)))
            test.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL)
            rows = ml.history(f.project, f.private)
            landed = any(r.state == "landed" and r.kind == "forward" for r in rows)
            reports = ml.recover(f.project, f.private)
            expected = f.expected(batch) if landed else f.before
            if not landed:
                expected = dict(expected)
                recorded = {p: identity for r in rows for p, identity in r.created_folders.items()}
                for event in batch.events:
                    path = f.project / event.path
                    if event.kind == "folder" and path.is_dir() and not recorded.get(event.path):
                        # A mkdir killed before its identity record must be kept (ML-20).
                        expected[event.path] = (stat.S_IFDIR, 0o755, None)
                        test.assertTrue(any(event.path == p.path for r in reports for p in r.paths))
            test.assertEqual(expected, tree(f.project))
            first = tree(f.project, control=True)
            records = ml.history(f.project, f.private)
            ml.recover(f.project, f.private)
            test.assertEqual(first, tree(f.project, control=True))
            test.assertEqual(records, ml.history(f.project, f.private))
            for report in reports:
                report_oracle(test, f, report, batch)
            if oracle:
                oracle(f, batch, trace.calls[boundary], rows)
        finally:
            f.close()


def fault_sweep(test, operation):
    discovery = Fixture()
    try:
        trace = Trace()
        operation(discovery, discovery.batch(), trace)
        points = [(i, c) for i, c in enumerate(trace.calls)
                  if c.when == "before" and c.operation in FAULTABLE]
        test.assertTrue(points)
    finally:
        discovery.close()
    for boundary, call in points:
        faults = (errno.ENOSPC, errno.EIO, "short") if call.operation == "write" else (errno.ENOSPC, errno.EIO)
        for fault in faults:
            f = Fixture()
            try:
                batch = f.batch()
                fired = []
                def inject(index, current):
                    if index == boundary:
                        fired.append(current)
                        if fault == "short":
                            data = current.detail["data"]
                            count = os.write(current.detail["fd"], data[:max(1, len(data) // 2)])
                            return ml.IOOverride(count)
                        raise OSError(fault, "ML injected filesystem/SQLite failure")
                    return None
                report = operation(f, batch, Trace(inject))
                test.assertTrue(fired)
                test.assertIn(report.state, ("refused", "reverted", "needs_owner", "landed"))
                if report.state == "landed":
                    # Only fully handled short writes or a reread-proven commit can succeed.
                    test.assertTrue(fault == "short" or call.operation == "sqlite_commit")
                if report.state == "needs_owner":
                    test.assertEqual("io", report.reason)
                    test.assertTrue(all(r.contents_kept for r in ml.history(f.project, f.private)))
                report_oracle(test, f, report, batch)
                ml.recover(f.project, f.private)
                ml.recover(f.project, f.private)
                if report.batch_no is not None and report.state == "needs_owner":
                    ml.retry(f.project, f.private, report.batch_no, policy=f.policy)
                test.assertIn(tree(f.project), (f.before, f.expected(batch)))
            finally:
                f.close()
