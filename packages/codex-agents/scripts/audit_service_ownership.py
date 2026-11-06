#!/usr/bin/env python3
"""Inventory source ownership without importing or executing the inspected code.

The boundary check is conservative static evidence, not a runtime call graph,
native authentication proof or proof that every normal client has been listed.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter, deque
from pathlib import Path, PurePosixPath

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "native-package/.codex/orchestration"
DEFAULT_CLIENT_ROOTS = (
    "cli.py", "learning_hook.py", "codex_agents/bridge.py",
    "codex_agents/collector_client.py", "codex_agents/history_api.py",
    "codex_agents/account_api.py",
)
PERSISTENCE_MODULES = frozenset({
    "sqlite3", "_sqlite3", "codex_agents.learning_store", "codex_agents.project_history",
    "codex_agents.history_service", "codex_agents.account_service",
})
SELECTED_METHODS = frozenset({"append", "_append_checked_locked", "_append_locked", "commit_owner_record"})
KEY_METHODS = frozenset({"keyed_digest", "external_id", "digest_key"})
SQL_METHODS = frozenset({
    "execute", "executemany", "executescript", "cursor", "fetchone", "fetchmany",
    "fetchall", "commit", "rollback", "set_trace_callback",
})
MAX_MODULES = 128
MAX_SOURCE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 8 * MAX_SOURCE_BYTES
MAX_AST_NODES = 1_000_000
MAX_AST_DEPTH = 128
MAX_CALLS = 20_000
MAX_IMPORTS = 8_192
MAX_REPORT_BYTES = 4 * MAX_SOURCE_BYTES
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")
_COMMAND = re.compile(r"[a-z][a-z0-9-]{0,79}")
REASONS = frozenset({"audit_invalid_path", "audit_invalid_source", "audit_source_limit", "audit_unsafe_source"})


class AuditError(RuntimeError):
    def __init__(self, reason: str):
        self.reason_code = reason if reason in REASONS else "audit_invalid_source"
        super().__init__(self.reason_code)


def source_name(name: str) -> PurePosixPath:
    if type(name) is not str or len(name) > 512 or "\\" in name:
        raise AuditError("audit_invalid_path")
    path = PurePosixPath(name)
    parts = name.split("/")
    if (
        path.is_absolute() or not parts or path.suffix != ".py"
        or any(part in {"", ".", ".."} for part in parts)
        or any(_IDENTIFIER.fullmatch(part) is None for part in (*parts[:-1], path.stem))
    ):
        raise AuditError("audit_invalid_path")
    return path


def module_name(path: str) -> str:
    parts = list(source_name(path).with_suffix("").parts)
    if parts[-1] == "__init__" and len(parts) > 1:
        parts.pop()
    return ".".join(parts)


def _forbidden(module: str) -> bool:
    return any(module == owner or module.startswith(owner + ".") for owner in PERSISTENCE_MODULES)


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        if len(node.id) > 160:
            raise AuditError("audit_source_limit")
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        value = base + "." + node.attr
        if len(value) > 512:
            raise AuditError("audit_source_limit")
        return value
    return "<dynamic>"


def _exports(tree: ast.Module) -> set[str]:
    """Declared module names, without treating function/class locals as exports."""
    names = set()

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        for child in ast.iter_child_nodes(node):
            visit(child)
    for statement in tree.body:
        visit(statement)
    return names


class Scanner(ast.NodeVisitor):
    def __init__(self, path, tree, modules, packages, exports):
        self.path, self.tree = path, tree
        self.module = module_name(path)
        self.package = self.module if path.endswith("/__init__.py") else self.module.rpartition(".")[0]
        self.modules, self.packages, self.exports = modules, packages, exports
        self.local_roots = {name.split(".")[0] for name in modules}
        self.aliases = {}
        self.imports, self.unknowns, self.calls = [], [], []
        self.scope, self.commands = [], []
        # Bind import aliases module-wide for syntactic call attribution only.
        # This deliberately does not claim flow-sensitive object/type resolution.
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                base = self._base(node)
                for alias in node.names:
                    self.aliases[alias.asname or alias.name] = ".".join(filter(None, (base, alias.name)))

    def _base(self, node):
        if not node.level:
            return node.module or ""
        parts = self.package.split(".") if self.package else []
        if node.level > len(parts):
            return ""
        return ".".join(parts[:len(parts) - node.level + 1] + ([node.module] if node.module else []))

    def _qualified(self, node):
        value = _dotted(node)
        head, dot, tail = value.partition(".")
        return self.aliases.get(head, head) + (dot + tail if dot else "")

    def _unknown(self, node, reason):
        self.unknowns.append({"line": node.lineno, "reason": reason})

    def _import(self, node, target):
        if not target or any(_IDENTIFIER.fullmatch(part) is None for part in target.split(".")):
            self._unknown(node, "unresolved_import")
            return
        local = target in self.modules or target in self.packages
        local_top = target.split(".")[0] in self.local_roots
        missing = local_top and not local
        self.imports.append({"line": node.lineno, "target": target, "local": local, "missing": missing})
        if missing:
            self._unknown(node, "missing_local_import")
        pieces = target.split(".")
        for size in range(1, len(pieces)):
            parent = ".".join(pieces[:size])
            if parent in self.modules:
                self.imports.append({"line": node.lineno, "target": parent, "local": True, "missing": False})

    def visit_Import(self, node):
        for alias in node.names:
            self._import(node, alias.name)

    def visit_ImportFrom(self, node):
        base = self._base(node)
        if not base:
            self._unknown(node, "unresolved_relative_import")
            return
        self._import(node, base)
        for alias in node.names:
            if alias.name == "*":
                self._unknown(node, "wildcard_import")
            elif base + "." + alias.name in self.modules or base + "." + alias.name in self.packages:
                self._import(node, base + "." + alias.name)
            elif base in self.modules and alias.name not in self.exports[base]:
                self._unknown(node, "unresolved_local_symbol")
            elif base in self.packages and base not in self.modules:
                self._unknown(node, "unresolved_package_member")
            if (
                (base == "importlib" and alias.name == "import_module")
                or (base == "runpy" and alias.name in {"run_path", "run_module"})
                or (base == "builtins" and alias.name in {"__import__", "eval", "exec"})
            ):
                self._unknown(node, "dynamic_loading")

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id in {"__import__", "eval", "exec"}:
            self._unknown(node, "dynamic_loading")

    def visit_Attribute(self, node):
        if (
            node.attr in {"import_module", "exec_module", "load_module", "__import__", "run_path", "run_module"}
            or self._qualified(node) in {"builtins.eval", "builtins.exec"}
        ):
            self._unknown(node, "dynamic_loading")
        self.generic_visit(node)

    def visit_Call(self, node):
        raw = _dotted(node.func)
        qualified = self._qualified(node.func)
        if len(qualified) > 512 or len(".".join(self.scope)) > 1024:
            raise AuditError("audit_source_limit")
        method = node.func.attr if isinstance(node.func, ast.Attribute) else raw
        categories = []
        if isinstance(node.func, ast.Attribute) and method in SELECTED_METHODS:
            categories.append("selected_method")
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "ledger":
            categories.append("ledger_named_receiver")
        if method in KEY_METHODS or qualified.rsplit(".", 1)[-1] in KEY_METHODS:
            categories.append("key_derivation_candidate")
        if qualified.startswith(("sqlite3.", "_sqlite3.")) or qualified in {
            "codex_agents.learning_store.LearningLedger", "codex_agents.learning_store.LearningState",
            "codex_agents.project_history.ProjectHistory",
        }:
            categories.append("persistence_constructor_or_call")
        if isinstance(node.func, ast.Attribute) and method in SQL_METHODS:
            categories.append("possible_sql_call")
        if raw == "getattr" and node.args and self._qualified(node.args[0]) == "importlib":
            self._unknown(node, "dynamic_loading")
        self.calls.append({
            "line": node.lineno, "scope": ".".join(self.scope), "commands": sorted(set(self.commands)),
            "callee": raw, "import_qualified_candidate": qualified, "categories": categories,
        })
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_If(self, node):
        self.visit(node.test)
        labels = []
        for part in ast.walk(node.test):
            if (
                isinstance(part, ast.Compare) and _dotted(part.left) == "args.command"
                and len(part.ops) == 1 and isinstance(part.ops[0], (ast.Eq, ast.In))
            ):
                for candidate in part.comparators:
                    values = candidate.elts if isinstance(candidate, (ast.List, ast.Tuple, ast.Set)) else [candidate]
                    labels.extend(value.value for value in values if isinstance(value, ast.Constant)
                                  and type(value.value) is str and _COMMAND.fullmatch(value.value))
        old = self.commands
        self.commands = old + labels
        for statement in node.body:
            self.visit(statement)
        self.commands = old
        for statement in node.orelse:
            self.visit(statement)


def audit_sources(sources: dict[str, str], client_roots=DEFAULT_CLIENT_ROOTS) -> dict:
    if type(sources) is not dict or not 0 < len(sources) <= MAX_MODULES:
        raise AuditError("audit_source_limit")
    if any(type(path) is not str or type(value) is not str for path, value in sources.items()):
        raise AuditError("audit_invalid_source")
    if (
        type(client_roots) not in {list, tuple} or not 0 < len(client_roots) <= MAX_MODULES
        or any(type(path) is not str for path in client_roots)
    ):
        raise AuditError("audit_invalid_path")
    trees, paths, sizes, digests = {}, {}, [], {}
    try:
        for path, source in sorted(sources.items()):
            name = module_name(path)
            if name in paths or type(source) is not str:
                raise AuditError("audit_invalid_source")
            raw = source.encode("utf-8")
            if len(raw) > MAX_SOURCE_BYTES:
                raise AuditError("audit_source_limit")
            sizes.append(len(raw))
            paths[name] = path
            digests[path] = hashlib.sha256(raw).hexdigest()
            trees[name] = ast.parse(source, filename="<source>")
    except (SyntaxError, UnicodeError, ValueError, RecursionError):
        raise AuditError("audit_invalid_source") from None
    if sum(sizes) > MAX_TOTAL_BYTES:
        raise AuditError("audit_source_limit")
    nodes = 0
    pending_nodes = [(tree, 0) for tree in trees.values()]
    while pending_nodes:
        node, depth = pending_nodes.pop()
        nodes += 1
        if nodes > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            raise AuditError("audit_source_limit")
        pending_nodes.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    packages = {piece for name in paths for piece in [".".join(name.split(".")[:n]) for n in range(1, len(name.split(".")))]}
    exports = {name: _exports(tree) for name, tree in trees.items()}
    modules = {}
    for name, tree in trees.items():
        scanner = Scanner(paths[name], tree, trees, packages, exports)
        scanner.visit(tree)
        modules[name] = {
            "path": paths[name], "sha256": digests[paths[name]],
            "imports": sorted({(v["line"], v["target"], v["local"], v["missing"]) for v in scanner.imports}),
            "unknowns": sorted({(v["line"], v["reason"]) for v in scanner.unknowns}),
            "calls": sorted(scanner.calls, key=lambda item: (item["line"], item["callee"])),
        }
    counts = Counter(category for item in modules.values() for call in item["calls"] for category in call["categories"])
    counts["all_ast_calls"] = sum(len(item["calls"]) for item in modules.values())
    counts["import_edges"] = sum(len(item["imports"]) for item in modules.values())
    if counts["all_ast_calls"] > MAX_CALLS or counts["import_edges"] > MAX_IMPORTS:
        raise AuditError("audit_source_limit")
    roots = tuple(sorted(set(client_roots)))
    if not roots or len(roots) > MAX_MODULES:
        raise AuditError("audit_invalid_path")
    clients = []
    for path in roots:
        name = module_name(path)
        violations, seen = [], set()
        pending = deque([(name, (name,))])
        if name not in modules or paths[name] != path:
            violations.append({"reason": "missing_client_root", "path": path, "line": 0, "via": [name]})
            pending.clear()
        while pending:
            current, via = pending.popleft()
            if current in seen:
                continue
            seen.add(current)
            if _forbidden(current):
                violations.append({"reason": "persistence_dependency", "path": paths.get(current, current), "line": 0, "via": list(via)})
                continue
            item = modules.get(current)
            if item is None:
                continue
            for line, reason in item["unknowns"]:
                violations.append({"reason": reason, "path": item["path"], "line": line, "via": list(via)})
            for call in item["calls"]:
                if "possible_sql_call" in call["categories"]:
                    violations.append({"reason": "possible_sql_call", "path": item["path"], "line": call["line"], "via": list(via)})
            parents = [".".join(current.split(".")[:n]) for n in range(1, len(current.split(".")))]
            targets = {v[1] for v in item["imports"] if v[2] or _forbidden(v[1])}
            targets.update(parent for parent in parents if parent in modules)
            pending.extend((target, via + (target,)) for target in sorted(targets) if target not in seen)
        clients.append({"root": path, "violations": violations})
    tree_material = "".join(path + "\0" + digest + "\n" for path, digest in sorted(digests.items()))
    report = {
        "schema_version": 1, "source_tree_digest": hashlib.sha256(tree_material.encode()).hexdigest(),
        "coverage": "declared_static_import_closure_and_syntactic_call_sites",
        "limitations": [
            "Not a flow-sensitive type/call graph or native authentication/OS security proof.",
            "Alias attribution is module-wide; dynamic object flows and external package internals are not resolved.",
            "Only the listed client roots are checked; this is not a completed normal-client census.",
            "Recognized dynamic-loading references and possible SQL calls conservatively fail client conformance.",
            "Call arguments and inspected code are never executed or included in the report.",
        ],
        "counts": dict(sorted(counts.items())), "boundary_conformant": not any(v["violations"] for v in clients),
        "count_definitions": {
            "all_ast_calls": "All ast.Call nodes across every supplied module, not executed calls.",
            "selected_method": "Attribute-call names append/_append_checked_locked/_append_locked/commit_owner_record, without receiver type proof.",
            "ledger_named_receiver": "Attribute calls whose receiver is exactly the name ledger, without object type proof.",
            "key_derivation_candidate": "Syntactic or imported-alias call names keyed_digest/external_id/digest_key, not a complete derivation flow.",
            "possible_sql_call": "Conservative SQL-like method names, including unknown receiver types.",
            "persistence_constructor_or_call": "Calls attributed to imported SQLite or known persistence constructors; not runtime type proof.",
            "import_edges": "Distinct module import targets by source line, including parent initializers.",
        },
        "clients": clients, "modules": modules,
    }
    if len(json.dumps(report, sort_keys=True).encode()) > MAX_REPORT_BYTES:
        raise AuditError("audit_source_limit")
    return report


def load_sources(root: Path) -> dict[str, str]:
    """Read bounded regular files from one explicit source tree, never follow links."""
    failure = None
    sources = {}
    total_bytes = entries = 0
    def walk_error(_):
        raise AuditError("audit_unsafe_source")
    try:
        root = root.absolute()
        if not root.is_dir() or root.resolve(strict=True) != root:
            raise AuditError("audit_unsafe_source")
        for directory, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
            entries += len(directories) + len(files)
            if entries > MAX_IMPORTS:
                raise AuditError("audit_source_limit")
            directories[:] = sorted(name for name in directories if name != "__pycache__")
            if any((Path(directory) / name).is_symlink() for name in directories):
                raise AuditError("audit_unsafe_source")
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = Path(directory) / name
                before = path.lstat()
                if not stat.S_ISREG(before.st_mode):
                    raise AuditError("audit_unsafe_source")
                descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as stream:
                    actual = os.fstat(stream.fileno())
                    if not stat.S_ISREG(actual.st_mode) or (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
                        raise AuditError("audit_unsafe_source")
                    raw = stream.read(MAX_SOURCE_BYTES + 1)
                if len(raw) > MAX_SOURCE_BYTES or len(sources) >= MAX_MODULES:
                    raise AuditError("audit_source_limit")
                total_bytes += len(raw)
                if total_bytes > MAX_TOTAL_BYTES:
                    raise AuditError("audit_source_limit")
                sources[path.relative_to(root).as_posix()] = raw.decode("utf-8")
        if sum(len(value.encode()) for value in sources.values()) > MAX_TOTAL_BYTES:
            raise AuditError("audit_source_limit")
    except (OSError, UnicodeError):
        failure = "audit_unsafe_source"
    if failure:
        raise AuditError(failure)
    return sources


def summary(report: dict) -> dict:
    return {
        key: report[key] for key in ("schema_version", "source_tree_digest", "coverage", "limitations", "counts", "count_definitions", "boundary_conformant")
    } | {
        "source_modules": len(report["modules"]),
        "clients": [{"root": item["root"], "violation_count": len(item["violations"]),
                     "reason_codes": sorted({v["reason"] for v in item["violations"]})} for item in report["clients"]],
        "full_report_option": "--full",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inventory", "check"), nargs="?", default="inventory")
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--client-root", action="append", dest="client_roots")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = audit_sources(load_sources(args.source_root), args.client_roots or DEFAULT_CLIENT_ROOTS)
        rendered = json.dumps(report if args.full else summary(report), sort_keys=True, indent=2)
        if len(rendered.encode()) + 1 > MAX_REPORT_BYTES:
            raise AuditError("audit_source_limit")
    except AuditError as exc:
        print(json.dumps({"ready": False, "reason_code": exc.reason_code}, sort_keys=True))
        return 2
    print(rendered)
    return 1 if args.mode == "check" and not report["boundary_conformant"] else 0


if __name__ == "__main__":
    sys.exit(main())

