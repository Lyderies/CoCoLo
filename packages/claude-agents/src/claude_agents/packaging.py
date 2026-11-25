"""Native developer package compiler and conservative owner-invoked installer.

No live provider adapter, hooks, settings, launcher, credentials, or model route.
Use quiescent targets. The cooperative lock is not protection from hostile same-user
writers; any observed unknown mutation is preserved and blocks further operations.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .contracts import ROLES
from .validation import validate_native_inventory

MANIFEST = ".claude/claude-agents/install-manifest.json"
PENDING = ".claude/claude-agents/install-pending.json"
LOCK = ".claude/claude-agents/install.lock"
_MODULES = ("__init__.py", "contracts.py", "validation.py")
_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LIMIT = 262144
_CATALOGUE_LIMIT = 2_097_152  # previous-releases.json only; it grows with every release
_CONTRACTS = {
    "researcher": "Cite current primary evidence and preserve uncertainty.",
    "planner": "Trace each step to accepted requirements and observable exits.",
    "implementation_worker": "Return one bounded patch for the primary to apply.",
    "security_reviewer": "Report concrete boundary defects with evidence and severity.",
    "verification_worker": "Independently test assigned criteria without fixing failures.",
}


class PackageError(ValueError):
    """Content-free package failure code. `rollback_state` says, as a field, whether this run kept unfinished
    work (its pending marker) that the caller must leave alone; `writes` and `folders_created` are what this
    run left written (see _Ledger)."""

    def __init__(self, code: str, rollback_state: str | None = None) -> None:
        super().__init__(code)
        self.rollback_state = rollback_state or ("pending" if code == "rollback_pending" else "none")
        self.writes: list[dict] = []
        self.folders_created: list[dict] = []


def _fail(code: str) -> None:
    raise PackageError(code)


class _Ledger:
    """Every write this run made, as the package itself made it: per path, the sha256 before its first write and
    after its last one, and every folder it created with its identity. A path whose net state is unchanged is no
    write. The caller builds its undo journal from this report, never from reading files back."""

    def __init__(self) -> None:
        self.files: dict[str, list[str | None]] = {}
        self.modes: dict[str, int | None] = {}
        self.before_modes: dict[str, int | None] = {}
        self.folders: list[dict] = []

    def wrote(self, relative: str, before: bytes | None, after: bytes | None, mode: int | None,
              before_mode: int | None) -> None:
        digests = [None if content is None else hashlib.sha256(content).hexdigest() for content in (before, after)]
        if relative in self.files:
            self.files[relative][1] = digests[1]
        else:
            self.files[relative] = digests
            self.before_modes[relative] = before_mode
        self.modes[relative] = mode

    def made_folder(self, relative: str, info: os.stat_result) -> None:
        self.folders.append({"path": relative, "device": info.st_dev, "inode": info.st_ino})

    def left(self, relative: str) -> bool:
        """True when this run wrote `relative` and did not take it away again."""
        return relative in self.files and self.files[relative][1] is not None

    def report(self) -> dict:
        return {"writes": [{"path": path, "before": before, "after": after, "mode": self.modes[path]}
                           for path, (before, after) in sorted(self.files.items())
                           if before != after or self.before_modes[path] != self.modes[path]],
                "folders_created": list(self.folders)}


_LEDGER: _Ledger | None = None


def _record(relative: str, before: bytes | None, after: bytes | None, mode: int | None = None,
            before_mode: int | None = None) -> None:
    if _LEDGER is not None:
        _LEDGER.wrote(relative, before, after, mode, before_mode)


def _root(path: Path) -> int:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        _fail("invalid_root")
    descriptor = os.open("/", _FLAGS)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(part, _FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _parent(root: int, relative: str, create: bool = False) -> tuple[int, str]:
    parts = relative.split("/")
    if any(p in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", p)
           for p in parts):
        _fail("unsafe_path")
    descriptor = os.dup(root)
    try:
        for index, part in enumerate(parts[:-1]):
            made = False
            if create:
                try:
                    os.mkdir(part, 0o755, dir_fd=descriptor)
                    made = True
                except FileExistsError:
                    pass
            next_descriptor = os.open(part, _FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            if made and _LEDGER is not None:
                _LEDGER.made_folder("/".join(parts[:index + 1]), os.fstat(descriptor))
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def _read(root: int, relative: str, *, with_mode: bool = False, limit: int = _LIMIT):
    try:
        parent, name = _parent(root, relative)
    except FileNotFoundError:
        return None
    try:
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                _fail("unsafe_file")
            result = b""
            while len(result) <= limit:
                chunk = os.read(descriptor, min(65536, limit + 1 - len(result)))
                if not chunk:
                    return (result, stat.S_IMODE(info.st_mode)) if with_mode else result
                result += chunk
            _fail("oversized_file")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


_GIT_HEAD = re.compile(rb"(?:ref: refs/heads/[A-Za-z0-9_./-]+|[0-9a-f]{40}|[0-9a-f]{64})\n")
_GIT_LINK = re.compile(rb"gitdir: ([^\0\r\n]{1,4096})\n?")


def _linked_git_dir_exists(target: Path, pointer: bytes) -> bool:
    """Check, without opening it, that a linked worktree points at a Git directory with a HEAD."""
    git_dir = Path(os.fsdecode(pointer))
    if not git_dir.is_absolute():
        git_dir = target / git_dir
    try:
        return (stat.S_ISDIR(os.lstat(git_dir).st_mode)
                and stat.S_ISREG(os.lstat(git_dir / "HEAD").st_mode))
    except OSError:
        return False


def _is_git_target(root: int, target: Path) -> bool:
    """Accept a Git directory or a linked-worktree marker file; never follow either."""
    try:
        info = os.stat(".git", dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(info.st_mode):
        head = _read(root, ".git/HEAD")
        return head is not None and _GIT_HEAD.fullmatch(head) is not None
    if stat.S_ISREG(info.st_mode):
        marker = _read(root, ".git")
        match = _GIT_LINK.fullmatch(marker) if marker is not None else None
        return match is not None and _linked_git_dir_exists(target, match.group(1))
    return False


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            _fail("write_failed")
        view = view[written:]
    os.fsync(descriptor)


def _exclusive(root: int, relative: str, content: bytes) -> None:
    parent, name = _parent(root, relative, True)
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        _record(relative, None, content, stat.S_IMODE(os.fstat(descriptor).st_mode))
        try:
            _write_all(descriptor, content)
        finally:
            os.close(descriptor)
        os.fsync(parent)
    finally:
        os.close(parent)


def _set_file(root: int, relative: str, before: bytes | None,
              after: bytes | None) -> None:
    current, before_mode = _read(root, relative, with_mode=True) or (None, None)
    if current != before:
        _fail("owned_file_changed")
    parent, name = _parent(root, relative, after is not None)
    temporary = ".claude-agents-" + secrets.token_hex(16)
    created = None
    try:
        if after is None:
            os.unlink(name, dir_fd=parent)
            _record(relative, before, None, before_mode=before_mode)
        else:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | os.O_NOFOLLOW, 0o644, dir_fd=parent)
            try:
                created = os.fstat(descriptor)
                _write_all(descriptor, after)
            finally:
                os.close(descriptor)
            if _read(root, relative) != before:
                _fail("owned_file_changed")
            if before is None:
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            else:
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            _record(relative, before, after, stat.S_IMODE(created.st_mode), before_mode)
        os.fsync(parent)
    finally:
        if created is not None:
            try:
                current = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
        os.close(parent)


def _canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True, slots=True)
class Bundle:
    files: tuple[tuple[str, bytes], ...]
    manifest: bytes


# Native definition default per role (aliases the Claude client accepts). A route
# that names a model still wins; this only stops a bare native launch from
# inheriting the primary's model.
ROLE_MODELS = {
    "implementation_worker": "sonnet",
    "planner": "sonnet",
    "researcher": "sonnet",
    "verification_worker": "sonnet",
    "security_reviewer": "opus",
}


def compile_bundle(source_root: Path) -> Bundle:
    """Compile fixed paths from an owner-selected trusted source; never execute it."""
    root = None
    try:
        root = _root(source_root)
        def source(relative: str) -> bytes:
            content = _read(root, relative)
            if content is None:
                _fail("source_missing")
            return content
        project = tomllib.loads(source("pyproject.toml").decode())["project"]
        if (project["name"] != "claude-agents" or type(project["version"]) is not str
                or not re.fullmatch(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}", project["version"])):
            _fail("invalid_source_metadata")
        texts = [source("tests/fixtures/native/" + name + ".md").decode()
                 for name in sorted(ROLES)]
        fixtures = validate_native_inventory(texts)
        files = {}
        for fixture, text in zip(fixtures, texts, strict=True):
            role = fixture.name.removeprefix("claude_agents_")
            model = ROLE_MODELS.get(role)
            if model is None:
                _fail("role_model_missing")
            text = text.replace("\n---\n", f"\nmodel: {model}\nmaxTurns: 8\n---\n", 1)
            text = text.replace("Inert fixture only. Do not execute or install this definition.\n", "")
            text += (
                "\n" + _CONTRACTS[role] + "\n"
                "Use one assignment and one attempt. Do not retry or reroute.\n"
                "Return the assigned structured result with admission_state unverified.\n"
                "Keep unavailable usage null. Do not attest effect cessation or accept your result.\n"
                "The model field is only a default; it provides no model isolation.\n"
                "Only the host/controller may select models or grant capabilities.\n"
            )
            files[".claude/agents/" + fixture.name + ".md"] = text.encode()
        files[".claude/skills/claude-agents/SKILL.md"] = (
            "---\nname: claude-agents\n"
            "description: Apply bounded research planning patch review and verification workflows\n"
            "---\n\n"
            "Use one bounded native role at a time with exact scope and evidence criteria.\n"
            "Keep provider/model choice, tools, secrets and admission in the host/controller.\n"
            "Obtain separate verification before accepting a meaningful result.\n"
            "The package grants no permissions and supplies no durable provider adapter.\n"
            "Model and tool inheritance remain native behavior, not package isolation.\n"
            "Do not enable hooks, MCP, usage credits, metered APIs or Fable.\n"
        ).encode()
        for module in _MODULES:
            content = source("src/claude_agents/" + module)
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                             else [node.module])
                    if any(name not in {"__future__", "json", "re", "dataclasses",
                                        "typing", "contracts"} for name in names):
                        _fail("non_offline_helper")
            files[".claude/claude-agents/lib/claude_agents/" + module] = content
        metadata = {
            "schema_version": 1, "namespace": "claude-agents",
            "package_version": project["version"], "provider_execution_enabled": False,
            "native_discovery_verified": False, "model_isolation": "not_provided",
            "files": {p: hashlib.sha256(b).hexdigest() for p, b in sorted(files.items())},
        }
        metadata["bundle_sha256"] = hashlib.sha256(_canonical(metadata)).hexdigest()
        return Bundle(tuple(sorted(files.items())), _canonical(metadata))
    except (OSError, UnicodeError, KeyError, SyntaxError, ValueError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError("invalid_source") from None
    finally:
        if root is not None:
            os.close(root)


# The bundle_sha256 of every earlier release this installer accepts an upgrade from. It lives
# here, in installer code, so previous-releases.json cannot vouch for itself: a record whose
# digest is not listed is refused, however consistent the file is. When a release is cut, add
# its digest here from that release's own build output (or its signed tag), reviewed in the
# same change as its record; never copy it from an installed project, which may be tampered.
PINNED_RELEASE_DIGESTS = frozenset({
    "c3e80b3f92cc5e5728b848e384d820265f70b32031ed931b723f201c1b0309ae",  # 0.8.40
    "eb34209dacdac6d3fe00fcd8a3f4c055bb3cdf661683ea270abc527f95ff62d3",  # 0.8.41 (source 1e3f43ba); 0.8.42 (source 9f229d0c), 0.8.43 (source d2bb82a3), 0.8.44 (source 87005877), 0.8.45 (source 1b3e665a), 0.8.46 (source eb54e847), 0.8.47 (source a1d25560), 0.8.48 (source 42b05f81), 0.8.49 (source b5318440), 0.8.50 (source 5ab0a9d3) and 0.8.51 (source 0e1c713a), 0.8.52 (source 36b7ef5f), 0.8.53 (source 45c214cd), 0.8.54 (source e626977d), 0.8.55 (source 8d4a7ce3), 0.8.56 (source ace235a4), 0.8.57 (source 6a1ea9da), 0.8.58 (source 1c293c44), 0.8.59 (source 227b0c5e), 0.8.60 (source cbc757d5), 0.8.61 (source 782a7eed), 0.8.62 (source cebb9002), 0.8.63 (source 38a96538), 0.8.64 (source 30e0a8b7) compile to the same bundle
})


def bundle_paths() -> frozenset[str]:
    """The fixed set of paths compile_bundle produces (the manifest excluded); a recorded
    release may name only these."""
    return frozenset({
        *(".claude/agents/" + role + ".md" for role in ROLES),
        ".claude/skills/claude-agents/SKILL.md",
        *(".claude/claude-agents/lib/claude_agents/" + module for module in _MODULES),
    })


def released_bundles(source_root: Path) -> tuple[Bundle, ...]:
    """The earlier released bundles this source records (previous-releases.json). A record is
    kept only if its digest is pinned in this module, it names only bundle_paths(), and its
    recorded bundle_sha256 is the one its files produce; any other record is refused."""
    root = None
    try:
        root = _root(source_root)
        raw = _read(root, "previous-releases.json", limit=_CATALOGUE_LIMIT)
        if raw is None:
            return ()
        records = json.loads(raw.decode())["releases"]
        bundles = []
        for record in records:
            files = {path: content.encode() for path, content in record["files"].items()}
            if record["bundle_sha256"] not in PINNED_RELEASE_DIGESTS:
                _fail("previous_release_not_pinned")
            if not files or not set(files) <= bundle_paths():
                _fail("previous_release_path_refused")
            metadata = {
                "schema_version": 1, "namespace": "claude-agents",
                "package_version": record["package_version"], "provider_execution_enabled": False,
                "native_discovery_verified": False, "model_isolation": "not_provided",
                "files": {p: hashlib.sha256(b).hexdigest() for p, b in sorted(files.items())},
            }
            if hashlib.sha256(_canonical(metadata)).hexdigest() != record["bundle_sha256"]:
                _fail("invalid_previous_release")
            metadata["bundle_sha256"] = record["bundle_sha256"]
            bundles.append(Bundle(tuple(sorted(files.items())), _canonical(metadata)))
        return tuple(bundles)
    except (OSError, UnicodeError, KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError("invalid_previous_release") from None
    finally:
        if root is not None:
            os.close(root)


def _plan(root: int, bundle: Bundle, previous: Bundle | tuple[Bundle, ...] | None, operation: str,
          locked: bool = False) -> list[tuple[str, bytes | None, bytes | None]]:
    if _read(root, PENDING) is not None or (not locked and _read(root, LOCK) is not None):
        _fail("installation_pending")
    manifest = _read(root, MANIFEST)
    old = None
    if manifest is not None:
        earlier = previous if isinstance(previous, tuple) else (previous,)
        old = next((b for b in (bundle, *earlier) if b is not None
                    and b.manifest == manifest), None)
        if old is None:
            _fail("untrusted_manifest")
    elif operation in {"verify", "update"}:
        _fail("not_installed")
    old_files = dict(old.files) if old else {}
    new_files = dict(bundle.files)
    for path in sorted(set(old_files) | set(new_files)):
        content = _read(root, path)
        if content != old_files.get(path):
            _fail("managed_file_modified" if old else "unmanaged_conflict")
    if operation in {"install", "verify"} and old and old.manifest != bundle.manifest:
        _fail("update_required")
    if operation == "verify":
        return []
    desired = {} if operation == "remove" else new_files
    result = [(p, old_files.get(p), desired.get(p))
              for p in sorted(set(old_files) | set(new_files))
              if old_files.get(p) != desired.get(p)]
    after = None if operation == "remove" else bundle.manifest
    if manifest != after:
        result.append((MANIFEST, manifest, after))
    return result


def package_operation(source_root: Path, target: Path, operation: str, *,
                      apply: bool = False, previous_source_root: Path | None = None) -> dict:
    """Preview by default. Update/rollback requires exact trusted old bundle bytes.

    An applied operation's report, and every failure (PackageError), carries this run's own write report
    (`writes`, `folders_created`, see _Ledger); a failure that leaves this run's pending marker in place says
    rollback_state "pending"."""
    global _LEDGER
    if type(operation) is not str or operation not in {"install", "update", "verify", "remove"}:
        _fail("invalid_operation")
    if type(apply) is not bool:
        _fail("invalid_apply")
    ledger, outer = _Ledger(), _LEDGER
    _LEDGER = ledger
    try:
        report = _operate(source_root, target, operation, apply, previous_source_root)
    except PackageError as exc:
        exc.writes, exc.folders_created = ledger.report()["writes"], ledger.report()["folders_created"]
        if ledger.left(PENDING):  # its marker is still there: unfinished work the caller must leave alone
            exc.rollback_state = "pending"
        raise
    finally:
        _LEDGER = outer
    if apply and operation != "verify":
        report.update(ledger.report())
    return report


def _operate(source_root: Path, target: Path, operation: str, apply: bool,
             previous_source_root: Path | None) -> dict:
    bundle = compile_bundle(source_root)
    previous = ((compile_bundle(previous_source_root),) if previous_source_root else ()) \
        + released_bundles(source_root)
    root = None
    token = secrets.token_hex(16).encode()
    lock_owned = False
    try:
        root = _root(target)
        if not _is_git_target(root, target):
            _fail("git_target_required")
        changes = _plan(root, bundle, previous, operation)
        report = {
            "operation": operation, "changed_paths": [p for p, _, _ in changes],
            "provider_execution_enabled": False, "native_discovery_verified": False,
            "result": "verified" if operation == "verify" else "preview",
        }
        if not apply or operation == "verify" or not changes:
            if apply and not changes and operation != "verify":
                report["result"] = "unchanged"
            return report
        _exclusive(root, LOCK, token)
        lock_owned = True
        changes = _plan(root, bundle, previous, operation, locked=True)
        report["changed_paths"] = [p for p, _, _ in changes]
        if not changes:
            report["result"] = "unchanged"
            return report
        marker = _canonical({
            "schema_version": 1, "operation": operation, "token": token.decode(),
            "changes": [{"path": p,
                         "before": None if b is None else hashlib.sha256(b).hexdigest(),
                         "after": None if a is None else hashlib.sha256(a).hexdigest()}
                        for p, b, a in changes],
        })
        _exclusive(root, PENDING, marker)
        attempted = []
        try:
            for change in changes:
                attempted.append(change)
                _set_file(root, *change)
            _set_file(root, PENDING, marker, None)
        except BaseException:
            incomplete = False
            for path, before, after in reversed(attempted):
                try:
                    current = _read(root, path)
                    if current == before:
                        continue
                    if current != after:
                        incomplete = True
                    else:
                        _set_file(root, path, after, before)
                except (OSError, PackageError):
                    incomplete = True
            try:  # the marker stays exactly while work is unfinished; failing to settle it means unfinished
                if incomplete and _read(root, PENDING) is None:
                    _exclusive(root, PENDING, marker)
                if not incomplete and _read(root, PENDING) == marker:
                    _set_file(root, PENDING, marker, None)
            except (OSError, PackageError):
                incomplete = True
            raise PackageError("rollback_pending" if incomplete else "operation_failed",
                               rollback_state="pending" if incomplete else "none") from None
        report["result"] = "applied"
        return report
    except OSError:
        raise PackageError("unsafe_or_unavailable_path") from None
    finally:
        if root is not None:
            if lock_owned:
                try:
                    if _read(root, LOCK) == token:
                        _set_file(root, LOCK, token, None)
                except (OSError, PackageError):
                    pass
            os.close(root)
