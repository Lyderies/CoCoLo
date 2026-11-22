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
        self.owner_versions = {}
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
        self.operation_before = self.before
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
                and receipts.get(batch.child_id) == signature
                and self.verify_records(self.private, batch))

    @staticmethod
    def write_records(root, batch):
        """Actual stores for the fixture authority adapter (not production schemas)."""
        values = {"ledger.json": batch.batch_digest, "signatures.json": batch.approval,
                  "approvals.json": batch.approval}
        for filename, value in values.items():
            path = root / filename
            records = json.loads(path.read_text()) if path.exists() else {}
            records[batch.child_id] = value
            path.write_bytes(canonical(records))

    @staticmethod
    def verify_records(root, batch):
        return all(json.loads((root / name).read_text()).get(batch.child_id) == value
                   for name, value in (("ledger.json", batch.batch_digest),
                                       ("signatures.json", batch.approval),
                                       ("approvals.json", batch.approval)))

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
        self.write_records(self.private, batch)
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


def version_at(path):
    info = Path(path).lstat()
    return (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
            Path(path).read_bytes() if stat.S_ISREG(info.st_mode) else None)


def report_oracle(test, fixture, report, batch, *, retained=None, moved=None):
    """Independently verify classes, retained bytes/modes and moved identities."""
    retained, moved = retained or {}, moved or {}
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
        if name in moved:
            destination, identity = moved[name]
            info = Path(destination).lstat()
            test.assertEqual(identity, (info.st_dev, info.st_ino))
            test.assertFalse(path.exists())
            kind = "moved"
        else:
            kind = ("unknown" if observed == "unknown" else "absent" if observed is None
                    else "base" if observed == base else "new" if observed == new else "owner")
        test.assertEqual(kind, listed[name].classification, name)
        allowed = {(stat.S_IFREG, v.mode_bits, v.data) for v in (event.base, event.new) if v}
        allowed.update(fixture.owner_versions.get(name, ()))
        for saved in listed[name].retained:
            actual = version_at(saved)
            if saved in retained:
                test.assertEqual(retained[saved], actual, saved)
            else:
                test.assertIn(actual, allowed, saved)
    for saved, expected in retained.items():
        test.assertIn(saved, set(report.leftovers) | {p for r in report.paths for p in r.retained})
        test.assertEqual(expected, version_at(saved), saved)
    staging = fixture.project / ".cocolo/staging" / batch.child_id
    if staging.exists():
        actual = {str(p) for p in staging.rglob("*") if p.is_file() and p.name != ".gitignore"}
        named = set(report.leftovers) | {p for row in report.paths for p in row.retained}
        test.assertTrue(actual.issubset(named), actual - named)


def write_open(call):
    return call.operation == "open" and (
        bool(call.detail.get("mutating")) or bool(call.detail.get("flags", 0) & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)))


def assert_no_git_writes(test, fixture, calls):
    anchor = fixture.project / ".git"
    for call in calls:
        mutating = call.operation in MUTATIONS | {"write", "sqlite_write", "ftruncate", "link", "chmod"}
        mutating |= bool(call.detail.get("mutating"))
        mutating |= write_open(call)
        if mutating:
            for value in (call.path, call.other):
                if value:
                    path = Path(os.path.abspath(fixture.project / value))
                    test.assertFalse(path == anchor or anchor in path.parents, call)


def assert_trace(test, calls):
    """Derive durability obligations from actual writes/mutations, not commit metadata."""
    before = [(i, c) for i, c in enumerate(calls) if c.when == "before"]
    commits = [(i, c) for i, c in before if c.operation == "sqlite_commit"
               and c.detail.get("commit") not in ("queue", "retire")]
    test.assertEqual(["A", "B"], [c.detail["commit"] for _, c in commits])
    a, b = [i for i, _ in commits]
    for i, call in before:
        if call.operation == "sqlite_commit" and call.detail.get("commit") == "queue":
            test.assertTrue(i < a or i > b, "admission transactions are outside A/B")
        if call.operation == "sqlite_commit" and call.detail.get("commit") == "retire":
            test.assertGreater(i, b, "retirement is post-terminal cleanup")
    a_done = next(i for i, c in enumerate(calls) if c.operation == "sqlite_commit"
                  and c.when == "after" and c.detail.get("commit") == "A")
    mutations = [i for i, c in before if c.detail.get("scope") == "tree"
                 and (c.operation in MUTATIONS or write_open(c))
                 and c.phase != "cleanup"]
    test.assertTrue(mutations)
    test.assertLess(a_done, min(mutations))
    test.assertLess(max(mutations), b)
    checked = [i for i, c in enumerate(calls) if c.operation == "project_checks" and c.when == "after"
               and c.result is True]
    test.assertTrue(checked)
    test.assertLess(max(checked), b)
    assert_commit_flushes(test, calls)


def assert_commit_flushes(test, calls):
    """ML-19: every transaction, including queue/retire/undo/reverse, has fresh durability."""
    commits = [(i, c) for i, c in enumerate(calls)
               if c.when == "before" and c.operation == "sqlite_commit"]
    previous_commit = -1
    for index, _ in commits:
        dirty = {}
        history_writes = []
        for n, call in enumerate(calls[:index]):
            if call.when != "after":
                continue
            if call.operation in ("write", "sqlite_write", "ftruncate", "chmod"):
                test.assertTrue(call.path, call)
                dirty[call.path] = n
                if call.detail.get("scope") == "history" and n > previous_commit:
                    history_writes.append(n)
            if call.operation in MUTATIONS:
                for path in (call.path, call.other):
                    if path:
                        dirty[str(Path(path).parent)] = n
                if call.operation == "mkdir":
                    dirty[call.path] = n
            if write_open(call):
                test.assertTrue(call.path, call)
                dirty[call.path] = n
                dirty[str(Path(call.path).parent)] = n
        test.assertTrue(history_writes, "each commit must expose its own history writes")
        for path, last_change in dirty.items():
            flushes = [n for n, c in enumerate(calls[:index]) if n > last_change and c.path == path
                       and c.when == "after" and c.operation in (
                           ("fullfsync",) if sys.platform == "darwin" else ("fsync", "fullfsync"))]
            test.assertTrue(flushes, f"missing fresh full flush: {path}")
        previous_commit = index


def call_identity(call, fixture):
    def normalized(value):
        if value is None:
            return None
        path = Path(os.path.abspath(fixture.project / value))
        return path.relative_to(fixture.root).as_posix() if path.is_relative_to(fixture.root) else str(path)
    return (call.operation, call.phase, call.when, normalized(call.path), normalized(call.other),
            call.detail.get("commit"), call.detail.get("name"))


def replay_key(calls, index, fixture):
    identity = call_identity(calls[index], fixture)
    occurrence = sum(call_identity(c, fixture) == identity for c in calls[:index + 1])
    return identity, occurrence


class ReplayPoint:
    """Match a stable boundary and occurrence, independent of incidental call indices."""
    def __init__(self, key, fixture, action):
        self.key, self.fixture, self.action = key, fixture, action
        self.seen = 0
        self.fired = []

    def __call__(self, index, call):
        if call_identity(call, self.fixture) == self.key[0]:
            self.seen += 1
            if self.seen == self.key[1]:
                self.fired.append(call)
                return self.action(call)
        return None

    def assert_fired(self, test):
        test.assertEqual(1, len(self.fired), self.key)


def assert_discovery(test, calls, operation):
    selected = [c for c in calls if c.operation != "checkpoint"]
    test.assertTrue(selected)
    for call in selected:
        test.assertTrue(any(c.operation == call.operation and c.phase == call.phase
                            and c.path == call.path and c.other == call.other
                            and c.when != call.when for c in selected), call)
    test.assertTrue(any(c.operation in MUTATIONS for c in selected))
    kind = operation.__name__
    phases = {c.phase for c in selected}
    required = {"forward": {"history", "staging", "forward", "validation"},
                "reverse": {"reverse"}, "recovering": {"recovery"},
                "cleaning": {"cleanup"}}.get(kind, set())
    test.assertTrue(required.issubset(phases), (kind, phases))
    if kind in ("forward", "undoing"):
        boundaries = {(c.detail.get("commit"), c.when) for c in selected if c.operation == "sqlite_commit"}
        labels = {"A", "B"} if kind == "forward" else {"undo"}
        test.assertTrue({(label, side) for label in labels for side in ("before", "after")}.issubset(boundaries))


def durable_endpoint(fixture, batch, rows, kind):
    """Expected recovery endpoint for this operation, using only durable history."""
    if kind in ("reverse", "recovering"):
        landed = False
    elif kind == "undoing":
        landed = not any(r.kind == "undo" and r.state == "landed" for r in rows)
    else:
        landed = any(r.kind == "forward" and r.state == "landed" for r in rows)
    return fixture.expected(batch) if landed else fixture.before


def outcome_oracle(test, f, batch, report, rows, kind):
    """Couple returned state, durable row and the operation's exact tree."""
    report_oracle(test, f, report, batch)
    matching = [r for r in rows if r.batch_no == report.batch_no]
    if report.recorded:
        test.assertTrue(matching)
        test.assertEqual(report.state, matching[0].state)
    if report.state == "landed":
        test.assertTrue(report.recorded)
        test.assertTrue(any(r.state == "landed" and r.kind == ("undo" if kind == "undoing" else "forward")
                            for r in matching))
        if kind == "undoing":
            test.assertTrue(any(r.kind == "forward" and r.state == "undone" for r in rows))
        expected = f.before if kind == "undoing" else f.expected(batch)
        test.assertEqual(expected, tree(f.project))
    elif report.state == "reverted":
        test.assertTrue(report.recorded, "reverted requires a durable terminal outcome")
        test.assertTrue(any(r.state == "reverted" for r in matching))
        test.assertEqual(f.expected(batch) if kind == "undoing" else f.before, tree(f.project))
    elif report.state == "refused":
        test.assertEqual(f.operation_before, tree(f.project))
    else:
        test.assertEqual("needs_owner", report.state)
        test.assertEqual("io", report.reason)
        test.assertTrue(rows, "unresolved work needs pinned history")
        test.assertTrue(all(r.contents_kept for r in rows))
        test.assertTrue(any(r.kind == "forward" and r.batch == batch for r in rows),
                        "pinned history must retain the exact sealed old/new contents and modes")
        actual, pre, post = tree(f.project), f.before, f.expected(batch, cleanup=False)
        for path in set(actual) | set(pre) | set(post):
            test.assertIn(actual.get(path), (pre.get(path), post.get(path)), path)


def fault_outcome_oracle(test, f, batch, report, rows, kind, call, fault):
    # ML-12: "I/O failure ends reverted or needs_owner(io), with history contents pinned".
    # A handled short write returns a count, not an I/O error. Only SQLite commit
    # uncertainty may resolve to success by reopening the durable terminal row
    # (protocol §9.1); ordinary swallowed EIO/ENOSPC must never count as landed.
    if report.state == "landed":
        test.assertTrue(fault == "short" or call.operation == "sqlite_commit")
    else:
        test.assertIn(report.state, ("reverted", "needs_owner"))
    outcome_oracle(test, f, batch, report, rows, kind)


def location_refusal_oracle(test, report, before, after):
    """ML-30: the otherwise-valid child authority must fail the location guard."""
    test.assertEqual("refused", report.state)
    test.assertEqual(before, after)


def owner_preservation_oracle(test, f, batch, reports, owner_tree):
    """Exact unrelated tree plus both explicitly named versions for unresolved work."""
    actual = tree(f.project)
    unrelated = {p: v for p, v in actual.items() if p != "a.txt"}
    test.assertEqual({p: v for p, v in owner_tree.items() if p != "a.txt"}, unrelated)
    unresolved = any(r.state in ("needs_owner", "waiting_owner") for r in reports)
    if not unresolved:
        test.assertEqual(owner_tree, actual)
        return
    rows = [row for r in reports for row in r.paths if row.path == "a.txt"]
    named = {p for row in rows for p in row.retained} | {p for r in reports for p in r.leftovers}
    versions = {version_at(p) for p in named if Path(p).is_file() and not Path(p).is_symlink()}
    owner = owner_tree["a.txt"]
    child = next(e.new for e in batch.events if e.path == "a.txt")
    test.assertIn(actual.get("a.txt"), (owner, (stat.S_IFREG, child.mode_bits, child.data)))
    test.assertIn(owner, versions | {actual.get("a.txt")})
    test.assertIn((stat.S_IFREG, child.mode_bits, child.data), versions | {actual.get("a.txt")})


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


def recover_checked(test, f, batch, kind):
    reports = ml.recover(f.project, f.private)
    rows = ml.history(f.project, f.private)
    for report in reports:
        row = next((r for r in rows if r.batch_no == report.batch_no), None)
        report_kind = "undoing" if row and row.kind == "undo" else kind
        outcome_oracle(test, f, batch, report, rows, report_kind)
    return reports


def kill_oracle(test, operation, *, select=lambda c: c.operation != "checkpoint", oracle=None):
    """Discover stable identities, then SIGKILL at every selected before/after call."""
    discovery = Fixture()
    try:
        batch = discovery.batch()
        trace = Trace()
        operation(discovery, batch, trace)
        assert_discovery(test, trace.calls, operation)
        assert_commit_flushes(test, trace.calls)
        boundaries = [(replay_key(trace.calls, i, discovery), c) for i, c in enumerate(trace.calls) if select(c)]
        test.assertTrue(boundaries, "a syscall hook must expose at least one boundary")
        if operation.__name__ == "forward" and any(c.operation == "sqlite_commit" for _, c in boundaries):
            test.assertTrue({(label, side) for label in ("A", "B") for side in ("before", "after")}
                            .issubset({(c.detail.get("commit"), c.when) for _, c in boundaries}))
    finally:
        discovery.close()
    for key, selected in boundaries:
        f = Fixture()
        try:
            batch = f.batch()
            # A durable marker proves identity matching even though SIGKILL loses Python memory.
            marker = f.root / "injection-fired"
            def kill(call):
                marker.write_bytes(canonical(key))
                os.kill(os.getpid(), signal.SIGKILL)
            point = ReplayPoint(key, f, kill)
            status = child_run(lambda: operation(f, batch, Trace(point)))
            test.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL)
            test.assertEqual(canonical(key), marker.read_bytes())
            rows = ml.history(f.project, f.private)
            expected = dict(durable_endpoint(f, batch, rows, operation.__name__))
            recorded = {p: identity for r in rows for p, identity in r.created_folders.items()}
            kept = []
            if expected == f.before:
                for event in batch.events:
                    path = f.project / event.path
                    if event.kind == "folder" and path.is_dir() and not recorded.get(event.path):
                        expected[event.path] = version_at(path)
                        kept.append(event.path)
            # Permit only specifically observed, unrecorded created folders in the pre-tree.
            for name in kept:
                f.before[name] = expected[name]
            reports = recover_checked(test, f, batch, operation.__name__)
            test.assertEqual(expected, tree(f.project))
            for name in kept:
                test.assertTrue(any(name == p.path for r in reports for p in r.paths))
            first = tree(f.project, control=True)
            records = ml.history(f.project, f.private)
            recover_checked(test, f, batch, operation.__name__)
            test.assertEqual(first, tree(f.project, control=True))
            test.assertEqual(records, ml.history(f.project, f.private))
            if oracle:
                oracle(f, batch, selected, rows)
        finally:
            f.close()


def fault_sweep(test, operation):
    discovery = Fixture()
    try:
        trace = Trace()
        operation(discovery, discovery.batch(), trace)
        assert_discovery(test, trace.calls, operation)
        assert_commit_flushes(test, trace.calls)
        points = [(replay_key(trace.calls, i, discovery), c) for i, c in enumerate(trace.calls)
                  if c.operation in FAULTABLE]
        test.assertTrue(points)
        # Both failed calls and successful calls with lost acknowledgement, including
        # undo/retire commits and fsync, get injected; short writes apply only before.
        for call in [c for _, c in points]:
            test.assertTrue(any(c.operation == call.operation and c.when != call.when for _, c in points))
    finally:
        discovery.close()
    for key, call in points:
        faults = (errno.ENOSPC, errno.EIO, "short") if call.operation == "write" and call.when == "before" else (errno.ENOSPC, errno.EIO)
        for fault in faults:
            f = Fixture()
            try:
                batch = f.batch()
                def inject(current):
                    if fault == "short":
                        data = current.detail["data"]
                        count = os.write(current.detail["fd"], data[:max(1, len(data) // 2)])
                        return ml.IOOverride(count)
                    raise OSError(fault, "ML injected filesystem/SQLite failure")
                point = ReplayPoint(key, f, inject)
                report = operation(f, batch, Trace(point))
                point.assert_fired(test)
                rows = ml.history(f.project, f.private)
                fault_outcome_oracle(test, f, batch, report, rows, operation.__name__, call, fault)
                recover_checked(test, f, batch, operation.__name__)
                first, records = tree(f.project, control=True), ml.history(f.project, f.private)
                recover_checked(test, f, batch, operation.__name__)
                test.assertEqual(first, tree(f.project, control=True))
                test.assertEqual(records, ml.history(f.project, f.private))
                unresolved = any(r.batch_no == report.batch_no and r.state in ("applying", "needs_owner")
                                 for r in records)
                if unresolved:
                    retry = ml.retry(f.project, f.private, report.batch_no, policy=f.policy)
                    outcome_oracle(test, f, batch, retry, ml.history(f.project, f.private), operation.__name__)
                rows = ml.history(f.project, f.private)
                test.assertEqual(durable_endpoint(f, batch, rows, operation.__name__), tree(f.project))
            finally:
                f.close()
