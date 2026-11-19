"""The wizard's effects (S5): the only code that reads or writes files or runs anything. Each ports
setup.sh exactly; `steps` decides what they mean."""

import importlib.util
import os
import stat
import subprocess
import tempfile
from pathlib import Path

CLONE = Path(__file__).resolve().parents[2]

PROFILE_READ_LIMIT = 65536  # One byte past it is read, so a larger file shows as larger.
ROUTING_READ_LIMIT = 1024 * 1024  # A routing file is read whole up to this; a larger one is refused.
HASH_LIMIT = 256 * 1024 * 1024  # A planned file larger than this is not hashed (it counts as changed).


class EffectRefused(RuntimeError):
    """An effect refused before changing anything; its message is fixed text, safe to show."""


def read_owned_file(path, limit: int = PROFILE_READ_LIMIT) -> bytes | None:
    """The bytes of a regular file the owner owns, without ever following a symlink or opening a FIFO
    or device; None when it is absent or refused. Only root-owned system links on the way (such as
    /var on macOS) are resolved. Ported from setup.sh's read_profile."""
    try:
        uid = os.geteuid()
        full = os.path.abspath(str(path))
        for _ in range(40):
            parts = [part for part in full.split("/") if part]
            current = "/"
            for index, part in enumerate(parts):
                current = os.path.join(current, part)
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    if index == len(parts) - 1 or os.lstat(current).st_uid != 0:
                        return None
                    full = os.path.join(os.path.realpath(current), *parts[index + 1:])
                    break
            else:
                break
        else:
            return None
        parts = [part for part in full.split("/") if part]
        if not parts:
            return None
        # Walk by directory descriptors, each opened relative to its checked parent without following
        # a link, so a swapped parent cannot redirect the read.
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = following
                if os.fstat(directory).st_uid not in (0, uid):
                    return None
            leaf = parts[-1]
            info = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
                return None
            descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != uid:
                    return None
                chunks, size = [], 0
                while size <= limit:
                    chunk = os.read(descriptor, min(65536, limit + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
    except OSError:
        return None


def read_routing(path) -> tuple[str, bytes | None]:
    """W4.5: ("absent", None) when there is no routing file, ("present", bytes) for a regular file read
    safely and whole, and ("refused", None) for one over ROUTING_READ_LIMIT, a symlink or any other
    kind of file, which is never opened."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "refused", None
    if not stat.S_ISREG(info.st_mode):
        return "refused", None
    raw = read_owned_file(path, ROUTING_READ_LIMIT)
    if raw is None:
        return "refused", None
    # Never decided from part of the file (review 040-s5-wizard d): larger than the limit is refused.
    return ("refused", None) if len(raw) > ROUTING_READ_LIMIT else ("present", raw)


def write_project_routing(project, content: str) -> None:
    """W4.4: write `.codex/orchestration/provider-routing.json` atomically, never through a symlink.
    Each folder is opened without following a link and held open, and every later step (the new
    file, its mode, the rename) is relative to it, so a folder swapped for a symlink after the checks
    cannot redirect the write (review 040-s5-wizard c). Refuses, changing nothing, when a folder on the
    way is a symlink or not a folder, or the destination is a symlink or not a regular file."""
    import secrets
    directory = os.open(os.path.realpath(project), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in (".codex", "orchestration"):
            try:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except OSError as exc:
                raise EffectRefused(f"refused: {part} is not a real directory inside the project") from exc
            os.close(directory)
            directory = following
        try:
            info = os.stat("provider-routing.json", dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise EffectRefused("refused: provider-routing.json is a symlink or not a regular file")
        temporary = f".provider-routing.{secrets.token_hex(6)}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "w") as handle:
                handle.write(content)
                handle.flush()
                os.fchmod(handle.fileno(), 0o644)
            os.replace(temporary, "provider-routing.json", src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass
            raise
    finally:
        os.close(directory)


_INSTALLER = None


def installer():
    """W1.8: this clone's scripts/install.py, loaded by its path (as setup.sh does), never found on
    sys.path, so nothing from the project or the current folder can stand in for it."""
    global _INSTALLER
    if _INSTALLER is None:
        spec = importlib.util.spec_from_file_location("agent_orchestration_install", CLONE / "scripts" / "install.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _INSTALLER = module
    return _INSTALLER


def git_helpers(project) -> list[str] | None:
    """W1.4: the project's own Git settings that name a program Git could start (none: []), read with
    plumbing; None when the settings cannot be read."""
    install = installer()
    try:
        return install.repository_helpers(Path(project))
    except (install.InstallRefused, OSError, subprocess.SubprocessError):
        return None


def git_changes(project, paths: list[str], why: list | None = None) -> list[str] | None:
    """W5.1, W5.2: the paths under `paths` with uncommitted changes (a rename's both names), literal
    paths only; None when Git could not answer or must not run (a helper named, W1.4). `why`, when
    given, receives the installer's reason (a fixed sentence) for a None."""
    if git_helpers(project) != []:
        return None
    install = installer()
    try:
        return install.project_changes(Path(project), list(paths))
    except install.InstallRefused as exc:
        if why is not None:
            why.append(str(exc))
        return None
    except (OSError, subprocess.SubprocessError, ValueError):
        if why is not None:
            why.append("Git could not be run")
        return None


# setup.sh's owner_git loader, word for word: install.py loaded by path, its git_tool run.
OWNER_GIT = ("import importlib.util, sys\n"
             "spec = importlib.util.spec_from_file_location(\"agent_orchestration_install\", sys.argv[1])\n"
             "module = importlib.util.module_from_spec(spec)\n"
             "spec.loader.exec_module(module)\n"
             "sys.exit(module.git_tool(sys.argv[2:]))")
PYF = ["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"]


def owner_git_argv(python: str, *args: str) -> list[str]:
    """setup.sh's owner_git: the installer's plumbing-only git tool, in the trusted Python, isolated.
    `commit PROJECT MESSAGE DETAIL PATH...` exits 0 committed, 3 nothing to commit, 2 refused (the
    reason on standard error), 1 failed."""
    return [python, *PYF, "-c", OWNER_GIT, str(CLONE / "scripts" / "install.py"), *args]


def git_commit(project, message: str, detail: str, paths: list[str], python: str | None = None) -> tuple[str, str]:
    """W5.3-W5.5: commit exactly `paths` with the installer's plumbing (no hook, filter or signing
    program runs), as a process (the wizard runs the same argv under its runner): ("committed", ""),
    ("nothing", "") when no path changed (W5.4: not a failure), or ("failed", the refusal, or "")."""
    import sys
    done = subprocess.run(owner_git_argv(python or sys.executable, "commit", str(project), message, detail, *paths),
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if done.returncode == 0:
        return "committed", ""
    if done.returncode == 3:
        return "nothing", ""
    return "failed", done.stderr.strip() if done.returncode == 2 else ""


_ATTEST = None


def attest():
    """W1.8: this clone's scripts/attest.py, loaded by its path, never found on sys.path."""
    global _ATTEST
    if _ATTEST is None:
        spec = importlib.util.spec_from_file_location("agent_orchestration_attest", CLONE / "scripts" / "attest.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ATTEST = module
    return _ATTEST






def runtime_problem(project) -> str:
    """W1.6: why the project's installed orchestrator is not this release byte for byte, or ""."""
    module = attest()
    try:
        module.check_project_runtime(Path(project))
    except module.AttestError as exc:
        return str(exc)
    except OSError:
        return "the orchestrator installed in this project could not be read"
    return ""








def unsafe_name(rel) -> bool:
    """A managed file name must be a plain, printable, relative UTF-8 path (setup.sh's helper)."""
    import unicodedata
    if not isinstance(rel, str) or not rel or rel[0] == " " or rel[-1] == " ":
        return True
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return True
    if any(unicodedata.category(char) in ("Cc", "Cs") for char in rel):
        return True
    return rel.startswith(("-", "/")) or ".." in rel.split("/")


def _digest(path) -> str | None:
    """A file's SHA-256, or None: never through a symlink, never a FIFO or device, read in chunks and
    only up to HASH_LIMIT (review 040-s5-wizard f). None counts as changed."""
    import hashlib
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > HASH_LIMIT:
            return None
        digest, size = hashlib.sha256(), 0
        while chunk := os.read(descriptor, 1024 * 1024):
            size += len(chunk)
            if size > HASH_LIMIT:
                return None
            digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _manifest(target: Path):
    import json
    raw = read_owned_file(target / ".codex" / "orchestration" / "install-manifest.json", ROUTING_READ_LIMIT)
    try:
        return json.loads(raw) if raw is not None and len(raw) <= ROUTING_READ_LIMIT else None
    except ValueError:
        return None


def plan_entries(reply, target, bundle) -> list[tuple[str, str | None]]:
    """Step 4's per-path plan from the install preview (setup.sh's `pj plan`): ADD, UPDATE, KEEP,
    LOCAL (written for this checkout only), REMOVE and UNTRACK (from the earlier manifest), CONFIG;
    ("UNSAFE", None) for a name that may not reach the screen."""
    target, bundle = Path(target), Path(bundle)
    reply = reply if isinstance(reply, dict) else {}
    codex, claude = reply.get("codex") or {}, reply.get("claude") or {}
    out: list[tuple[str, str | None]] = []
    files = codex.get("files") or []
    for rel in files:
        if unsafe_name(rel):
            out.append(("UNSAFE", None))
        elif not os.path.lexists(target / rel):
            out.append(("ADD", rel))
        elif (found := _digest(target / rel)) is not None and found == _digest(bundle / rel):
            out.append(("KEEP", rel))
        else:
            out.append(("UPDATE", rel))
    local = [rel for rel in (codex.get("local_files") or []) if isinstance(rel, str)]
    out.extend(("UNSAFE", None) if unsafe_name(rel) else ("LOCAL", rel) for rel in local)
    manifest = _manifest(target)
    old = manifest.get("files") if isinstance(manifest, dict) else None
    if isinstance(old, dict):
        for rel in sorted(set(old) - {name for name in files if isinstance(name, str)}):
            out.append(("UNSAFE", None) if unsafe_name(rel) else ("UNTRACK" if rel in local else "REMOVE", rel))
    settings = reply.get("claude_settings") or {}
    for rel in (claude.get("changed_paths") or []) + (settings.get("changed_paths") or []):
        if unsafe_name(rel):
            out.append(("UNSAFE", None))
        else:
            out.append(("UPDATE" if os.path.lexists(target / rel) else "ADD", rel))
    import re
    for rel, action in sorted((codex.get("project_configuration") or {}).items()):
        word = re.sub(r"[^A-Za-z0-9_ .+-]", "", str(action or ""))[:30] or "unknown"
        out.append(("UNSAFE", None) if unsafe_name(rel) else ("CONFIG", f"{rel} -> {word}"))
    return out


def plan_paths(entries) -> list[str] | None:
    """setup.sh's `pj planpaths`: the added, updated, removed and untracked paths, sorted; None when
    any name was unsafe."""
    if any(kind == "UNSAFE" for kind, _ in entries):
        return None
    return sorted({rel for kind, rel in entries if kind in ("ADD", "UPDATE", "REMOVE", "UNTRACK")})


def git_ignored(project, candidates: list[str]) -> list[str] | None:
    """W5.7: the candidates that exist untracked but are ignored by .gitignore (which a commit made by
    hand would refuse); None when Git could not answer, never read as "nothing is ignored"."""
    if not candidates:
        return []
    install = installer()
    try:
        result = install._git_run(Path(project), "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                                  "--", *candidates, text=False)
    except (OSError, subprocess.SubprocessError, install.InstallRefused):
        return None
    if result.returncode != 0:
        return None
    wanted = set(candidates)
    names = [item.decode("utf-8", "surrogateescape") for item in result.stdout.split(b"\0") if item]
    return [name for name in names if name in wanted]


def commit_candidates(project, reply, planned: list[str]) -> list[str]:
    """setup.sh's `pj paths`: what may be committed after the install: the planned paths, project
    configuration written (not preserved), Claude's changed paths, the manifest and the routing, each
    kept when it exists or is tracked. EffectRefused for an unsafe name or when Git cannot answer."""
    project = Path(project)
    reply = reply if isinstance(reply, dict) else {}
    codex, claude = reply.get("codex") or {}, reply.get("claude") or {}
    found = set(planned)
    found.update(rel for rel, action in (codex.get("project_configuration") or {}).items() if action != "preserve")
    found.update(claude.get("changed_paths") or [])
    found.update((reply.get("claude_settings") or {}).get("changed_paths") or [])
    found.update((".codex/orchestration/install-manifest.json", ".codex/orchestration/provider-routing.json"))
    if any(unsafe_name(rel) for rel in found):
        raise EffectRefused("unsafe file name in managed paths")
    install = installer()
    keep = []
    for rel in sorted(found):
        try:
            result = install._git_run(project, "ls-files", "-z", "--", rel, text=False)
        except (OSError, subprocess.SubprocessError, install.InstallRefused) as exc:
            raise EffectRefused("the path list could not be built") from exc
        if result.returncode != 0:
            raise EffectRefused("the path list could not be built")
        if os.path.lexists(project / rel) or result.stdout:
            keep.append(rel)
    return keep


def git_executable(untrusted) -> str | None:
    """W1.4: the git binary the runtime would choose (git_plumbing.git_executable): never macOS's xcrun
    shim, never one in a temporary or an untrusted folder; None when none qualifies."""
    try:
        return attest().clone_module("git_plumbing").git_executable([Path(item) for item in untrusted])
    except Exception:
        return None


def projects_file() -> Path | None:
    """W3.2: where setup keeps its list of projects: the records' installer folder, only when that is a
    real folder of yours that only you can open (attest.py projects-file)."""
    try:
        folder = attest().default_state_root() / "installer"
        info = folder.lstat()
    except Exception:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return None
    return folder / "setup-projects"


def read_projects(path) -> list[str]:
    """The remembered projects, one per line; nothing from a symlink or anything not a regular file."""
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def write_projects(path, lines: list[str]) -> None:
    """Replace the remembered list in one step: a new owner-only file renamed into place."""
    path = Path(path)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    except OSError:
        return
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write("".join(line + "\n" for line in lines))
        os.replace(temporary, path)
    except OSError:
        Path(temporary).unlink(missing_ok=True)


def copy_input(source, destination) -> bool:
    """A private snapshot of an owner-chosen input (attest.copy_input): a regular file of at most
    1 MiB, opened so a FIFO never hangs; False, writing nothing, otherwise."""
    module = attest()
    try:
        module.copy_input(Path(source), Path(destination))
        return True
    except (module.AttestError, OSError):
        return False


def legacy_report() -> dict | None:
    """W6.2: the old macOS records' report (attest.legacy_state_report), None when it cannot be read."""
    try:
        report = attest().legacy_state_report()
    except Exception:
        return None
    return report if isinstance(report, dict) else None


def owner_key_report() -> dict | None:
    """W7.3: the records' owner key report, None when the record cannot be read."""
    module = attest()
    try:
        store = module.clone_module("learning_store")
        return store.LearningState(module.default_state_root(), create=False).owner_key_report()
    except Exception:
        return None


def project_in_temp(project) -> bool:
    """Step 8: whether the project lies in a temporary folder a Codex session can write, decided with
    this clone's own host_paths, never the project's copy."""
    try:
        host_paths = attest().clone_module("host_paths")
        real = Path(os.path.realpath(project))
        return any(host_paths.within(real, root) for root in host_paths.temp_roots())
    except Exception:
        return False


def create_profile(folder, content: str) -> bool:
    """W8.1, step 7: create `orchestrator.config.toml` in the Codex home, new only (an existing file is
    never overwritten), mode 0600; every folder it creates is owner-only. All or nothing: a write that
    fails or is interrupted removes the new file (review 040-s5-wizard c, 040-s5-wizard-2 a)."""
    folder = Path(folder)
    path = folder / "orchestrator.config.toml"
    try:
        for missing in reversed([part for part in (folder, *folder.parents) if not os.path.lexists(part)]):
            missing.mkdir(mode=0o700)
            os.chmod(missing, 0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        return False
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(content + "\n")
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
        return True
    except BaseException as exc:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, OSError):
            return False
        raise
