"""Deterministic task-to-role, model, effort, and topology routing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import tomllib

from .admission_metadata import ProviderStateSnapshot
from .catalog import discover, load_discovery_policy
from .contracts import (
    FAILURE_REASONS,
    VERIFIED_FAILURE_REFERENCE,
    ContractError,
    validate_task,
)
from .git_plumbing import git_executable as find_git
from .git_plumbing import repository_layout_problem, run_git
from .learning_contracts import (
    content_digest,
    parse_bounded_json_value,
    parse_semver,
    provider_for_model,
    provider_for_role,
)


class PolicyError(ValueError):
    """Raised when routing policy is internally inconsistent."""


BOUND_FIELDS = {
    "max_attempts",
    "max_steers_per_attempt",
    "max_no_progress_events",
    "wait_seconds",
    "timeout_seconds",
    "max_tool_calls",
}

PROJECT_AGENT_MAPPING = Path(".codex/orchestration/project-agent-routing.json")
PROVIDER_ROUTING = Path(".codex/orchestration/provider-routing.json")
WRITABLE_PROJECT_FAMILIES = {"coding", "verification"}
PROVIDERS = ("codex", "claude")
ROUTE_KEYS = {"bounded", "exploratory", "judgment", "synthesis", "default"}
TOP_LEVEL_ROUTE_KEYS = ("reconnaissance", "hard_end_to_end")
CODEX_POLICY_CANDIDATE = {"provider": "codex", "route": "codex_policy"}
# Existing projects keep Codex-only routing until they commit a provider configuration.
DEFAULT_PROVIDER_ROUTING = {
    "version": 1,
    "providers": {"codex": {"enabled": True}, "claude": {"enabled": False}},
    "codex_primary_may_bridge_to_claude": False,
}
DEFAULT_PROVIDER_CONFIG_DIGEST = content_digest(DEFAULT_PROVIDER_ROUTING)
POLICY_KEYS = {
    "version", "status", "validated_client", "evaluation", "quality_first",
    "selection_policy", "max_parallel", "bounds", "observation_deadlines",
    "models", "legacy_fallback", "roles", "routes", "providers",
    "provider_candidates", "risk_candidates", "escalation", "verification",
    "quality_thresholds",
}
RISK_BANDS = ("low", "medium", "high", "critical")
HIGH_RISK_BANDS = ("high", "critical")
MAX_LADDER_RUNGS = 16


def _read_project_regular(root: Path, relative: str, *, max_bytes: int) -> bytes:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise PolicyError("project definition path is invalid")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        result = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor
        )
    except OSError as exc:
        raise PolicyError("project definition is unavailable") from exc
    finally:
        os.close(descriptor)
    try:
        metadata = os.fstat(result)
        if not stat.S_ISREG(metadata.st_mode):
            raise PolicyError("project definition is not a regular file")
        chunks: list[bytes] = []
        byte_count = 0
        while chunk := os.read(result, min(65_536, max_bytes + 1 - byte_count)):
            chunks.append(chunk)
            byte_count += len(chunk)
            if byte_count > max_bytes:
                raise PolicyError("project definition is too large")
        return b"".join(chunks)
    finally:
        os.close(result)


def _bind_committed_project_bytes(
    root: Path,
    contents: dict[str, bytes],
    *,
    subject: str = "project agent mapping and mapped agents",
) -> None:
    git_executable = find_git(untrusted=[root])
    if git_executable is None:
        raise PolicyError("project agent mapping trust is unavailable")
    if repository_layout_problem(git_executable, root) is not None:
        raise PolicyError(f"{subject} are not committed and clean")

    def run(arguments: list[str], *, text: bool = False) -> subprocess.CompletedProcess:
        return run_git(git_executable, root, *arguments, text=text, timeout=None)

    # Only plumbing: the committed blob is compared with the bytes the caller read
    # from the working tree, so no command refreshes the index or runs a helper.
    try:
        top = run(["rev-parse", "--show-toplevel"], text=True)
        head_before = run(["rev-parse", "--verify", "HEAD^{commit}"], text=True)
        commit = head_before.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise PolicyError(
                f"{subject} are not committed and clean"
            )
        for relative, expected in sorted(contents.items()):
            reference = f"{commit}:{relative}"
            size = run(["cat-file", "-s", reference], text=True)
            try:
                byte_count = int(size.stdout.strip())
            except ValueError:
                byte_count = -1
            blob = run(["cat-file", "blob", reference]) if (
                0 <= byte_count <= 1_048_576
            ) else None
            if (
                size.returncode != 0
                or blob is None
                or blob.returncode != 0
                or blob.stdout != expected
            ):
                raise PolicyError(
                    f"{subject} are not committed and clean"
                )
        head_after = run(["rev-parse", "--verify", "HEAD^{commit}"], text=True)
    except OSError as exc:
        raise PolicyError("project agent mapping trust is unavailable") from exc
    try:
        exact_root = Path(top.stdout.strip()).resolve(strict=True) == root.resolve(
            strict=True
        )
    except OSError:
        exact_root = False
    if (
        top.returncode != 0
        or not exact_root
        or head_before.returncode != 0
        or head_after.returncode != 0
        or head_after.stdout.strip() != commit
    ):
        raise PolicyError(
            f"{subject} are not committed and clean"
        )


def _committed_in_head(root: Path, relative: str) -> bool:
    """Return whether HEAD tracks a project file, failing closed when git is unsure.

    Only an unborn branch or a clean listing without the path allows defaults;
    any other git outcome, including a missing repository, raises.
    """

    unavailable = "project provider routing trust is unavailable"
    git_executable = find_git(untrusted=[root])
    if git_executable is None:
        raise ContractError(unavailable)
    if repository_layout_problem(git_executable, root) is not None:
        raise ContractError(unavailable)

    def run(arguments: list[str]) -> subprocess.CompletedProcess:
        return run_git(git_executable, root, *arguments, text=True, timeout=10)

    try:
        head = run(["rev-parse", "--verify", "--quiet", "HEAD"])
        if head.returncode == 1:
            return False
        if head.returncode != 0:
            raise ContractError(unavailable)
        listing = run(["ls-tree", "--name-only", "HEAD", "--", relative])
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(unavailable) from exc
    if listing.returncode != 0:
        raise ContractError(unavailable)
    names = listing.stdout.splitlines()
    if not names:
        return False
    if names == [relative]:
        return True
    raise ContractError(unavailable)


def validate_project_agent_mapping(
    root: Path,
    raw: bytes,
    policy_path: Path,
    discovery_policy_path: Path,
    *,
    require_committed: bool = False,
    fallback_root: Path | None = None,
) -> dict[str, Any]:
    """Validate one explicit mapping against agents in the exact project."""

    if len(raw) > 65_536:
        raise PolicyError("project agent mapping is too large")
    try:
        mapping = parse_bounded_json_value(raw, max_bytes=65_536)
    except ContractError as exc:
        raise PolicyError("project agent mapping is invalid") from exc
    if not isinstance(mapping, dict) or not set(mapping) <= {
        "version", "roles", "verifiers"
    } or set(mapping) < {"version", "roles"}:
        raise PolicyError(
            "project agent mapping must contain version and roles, with optional verifiers"
        )
    roles = mapping["roles"]
    verifiers = mapping.get("verifiers", {})
    if (
        mapping["version"] != 1
        or not isinstance(roles, dict)
        or not isinstance(verifiers, dict)
    ):
        raise PolicyError("project agent mapping version, roles, or verifiers are invalid")

    policy = load_policy(policy_path)
    supported = set(policy["roles"])
    unknown_roles = sorted(set(roles) - supported)
    unknown_verifiers = sorted(set(verifiers) - (supported - {"verification"}))
    if unknown_roles or unknown_verifiers:
        raise PolicyError("project agent mapping contains an unsupported task family")
    if any(
        not isinstance(agent_type, str) or not agent_type
        for agent_type in (*roles.values(), *verifiers.values())
    ):
        raise PolicyError("project agent mapping values must be non-empty strings")

    discovery_policy = load_discovery_policy(discovery_policy_path)
    catalog = discover(root, discovery_policy)
    if not catalog["ready"]:
        raise PolicyError("project agent catalog is not ready")
    selected: dict[str, tuple[dict[str, Any], Path]] = {}
    for item in catalog["selected"]:
        if item["kind"] == "agent" and item["format"] == "codex-toml":
            selected[item["name"]] = (item, root)
    claude_selected = {
        item["name"]: item
        for item in catalog["selected"]
        if item["kind"] == "agent" and item["format"] == "claude-md"
    }
    if fallback_root is not None:
        fallback = fallback_root.resolve(strict=True)
        fallback_catalog = discover(fallback, discovery_policy)
        if not fallback_catalog["ready"]:
            raise PolicyError("package agent catalog is not ready")
        for item in fallback_catalog["selected"]:
            if item["kind"] == "agent" and item["format"] == "codex-toml":
                selected.setdefault(item["name"], (item, fallback))
    selected_paths: set[str] = set()
    definition_contents: dict[str, bytes] = {}
    role_sandboxes: dict[str, str] = {}
    verifier_sandboxes: dict[str, str] = {}
    role_skill_paths: dict[str, list[str]] = {}
    verifier_skill_paths: dict[str, list[str]] = {}
    agent_paths: dict[str, str] = {}

    def selected_skill_paths(
        agent_path: str, definition_root: Path, expected_digest: str
    ) -> tuple[list[str], dict[str, bytes]]:
        try:
            agent_bytes = _read_project_regular(
                definition_root, agent_path, max_bytes=1_048_576
            )
            if hashlib.sha256(agent_bytes).hexdigest() != expected_digest:
                raise PolicyError("mapped project agent changed during validation")
            data = tomllib.loads(agent_bytes.decode("utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise PolicyError("mapped project agent skills are invalid") from exc
        skill_config = data.get("skills", [])
        configured_paths: list[Path] = []
        if isinstance(skill_config, list):
            configured_paths = [
                definition_root / ".agents" / "skills" / name / "SKILL.md"
                for name in skill_config
            ]
        elif isinstance(skill_config, dict) and set(skill_config) == {"config"}:
            for entry in skill_config["config"]:
                if not entry.get("enabled", True):
                    continue
                configured = Path(entry["path"])
                configured_paths.append(
                    configured
                    if configured.is_absolute()
                    else definition_root / configured
                )
        else:
            raise PolicyError("mapped project agent skills are invalid")

        resolved_paths: list[str] = []
        trust_contents: dict[str, bytes] = {agent_path: agent_bytes}
        for configured in configured_paths:
            try:
                configured_relative = configured.absolute().relative_to(
                    definition_root.absolute()
                )
                relative_text = configured_relative.as_posix()
                content = _read_project_regular(
                    definition_root, relative_text, max_bytes=1_048_576
                )
            except (OSError, PolicyError, ValueError) as exc:
                raise PolicyError("mapped project agent skill is unavailable") from exc
            if not relative_text.endswith("/SKILL.md"):
                raise PolicyError("mapped project agent skill is unavailable")
            resolved_paths.append(relative_text)
            trust_contents[relative_text] = content
        return sorted(set(resolved_paths)), trust_contents

    def validate_agent(
        family: str, agent_type: str, *, verifier: bool
    ) -> list[str]:
        selection = selected.get(agent_type)
        if selection is None:
            raise PolicyError(f"mapped project agent is unavailable: {agent_type}")
        candidate, definition_root = selection
        if candidate["source"] != "project":
            raise PolicyError(f"mapped project agent is unavailable: {agent_type}")
        candidate_path = definition_root / candidate["path"]
        current = definition_root
        try:
            relative_candidate = candidate_path.absolute().relative_to(
                definition_root.absolute()
            )
        except ValueError as exc:
            raise PolicyError(f"mapped project agent is unavailable: {agent_type}") from exc
        for part in relative_candidate.parts:
            current /= part
            if current.is_symlink():
                raise PolicyError(
                    f"mapped project agent must not be a symlink: {agent_type}"
                )
        has_unreviewed_keys = any(
            warning.startswith("unreviewed Codex config keys:")
            for warning in candidate["warnings"]
        )
        if (
            candidate["errors"]
            or candidate["requested_mcps"]
            or candidate["requested_tools"]
            or has_unreviewed_keys
        ):
            raise PolicyError(f"mapped project agent is not executable: {agent_type}")
        if (
            candidate["configured_model"] is not None
            or candidate["configured_effort"] is not None
        ):
            raise PolicyError(
                f"mapped project agent must not override model or effort: {agent_type}"
            )
        allowed_sandboxes = (
            {"read-only", "workspace-write"}
            if verifier or family in WRITABLE_PROJECT_FAMILIES
            else {"read-only"}
        )
        if candidate["sandbox_mode"] not in allowed_sandboxes:
            raise PolicyError(
                f"mapped project agent has an unauthorized sandbox: {agent_type}"
            )
        selected_paths.add(candidate["path"])
        agent_paths[agent_type] = candidate["path"]
        candidate_digest = candidate.get("content_sha256")
        if not isinstance(candidate_digest, str):
            raise PolicyError(f"mapped project agent is unavailable: {agent_type}")
        skill_paths, skill_trust_contents = selected_skill_paths(
            candidate["path"], definition_root, candidate_digest
        )
        selected_paths.update(skill_trust_contents)
        definition_contents.update(skill_trust_contents)
        return skill_paths
    claude_roles: dict[str, str] = {}
    claude_role_sandboxes: dict[str, str] = {}

    def validate_claude_agent(agent_type: str) -> str:
        # A mapped Claude agent only names the definition; model and effort stay
        # with policy, and write tools leave it ineligible rather than writable.
        candidate = claude_selected[agent_type]
        expected_path = f".claude/agents/{agent_type}.md"
        if candidate["source"] != "project" or candidate["path"] != expected_path:
            raise PolicyError(f"mapped project agent is unavailable: {agent_type}")
        current = root
        for part in Path(expected_path).parts:
            current /= part
            if current.is_symlink():
                raise PolicyError(
                    f"mapped project agent must not be a symlink: {agent_type}"
                )
        if candidate["errors"] or candidate["warnings"]:
            raise PolicyError(f"mapped project agent is not executable: {agent_type}")
        # A model alias is only the definition's fallback; the route's model wins.
        if candidate["configured_model"] not in (None, "sonnet", "opus", "haiku"):
            raise PolicyError(
                f"mapped project agent must not override model or effort: {agent_type}"
            )
        try:
            agent_bytes = _read_project_regular(
                root, expected_path, max_bytes=1_048_576
            )
        except PolicyError as exc:
            raise PolicyError(
                f"mapped project agent is unavailable: {agent_type}"
            ) from exc
        if hashlib.sha256(agent_bytes).hexdigest() != candidate["content_sha256"]:
            raise PolicyError("mapped project agent changed during validation")
        selected_paths.add(expected_path)
        definition_contents[expected_path] = agent_bytes
        agent_paths[agent_type] = expected_path
        return candidate["sandbox_mode"] or "write-tools"

    for family, agent_type in roles.items():
        if agent_type in claude_selected:
            if agent_type in selected:
                raise PolicyError(
                    f"mapped project agent name is ambiguous: {agent_type}"
                )
            claude_roles[family] = agent_type
            claude_role_sandboxes[family] = validate_claude_agent(agent_type)
            continue
        role_skill_paths[family] = validate_agent(
            family, agent_type, verifier=False
        )
        role_sandboxes[family] = selected[agent_type][0]["sandbox_mode"]
    for family, agent_type in verifiers.items():
        verifier_skill_paths[family] = validate_agent(
            family, agent_type, verifier=True
        )
        verifier_sandboxes[family] = selected[agent_type][0]["sandbox_mode"]
    if require_committed:
        _bind_committed_project_bytes(
            root,
            {PROJECT_AGENT_MAPPING.as_posix(): raw, **definition_contents},
        )
    definition_hasher = hashlib.sha256()
    definition_hasher.update(raw)
    for relative in sorted(selected_paths):
        content = definition_contents[relative]
        definition_hasher.update(relative.encode("utf-8"))
        definition_hasher.update(b"\0")
        definition_hasher.update(content)
        definition_hasher.update(b"\0")
    return {
        "roles": {
            family: agent_type
            for family, agent_type in roles.items()
            if family not in claude_roles
        },
        "claude_roles": claude_roles,
        "claude_role_sandboxes": claude_role_sandboxes,
        "verifiers": dict(verifiers),
        "role_sandboxes": role_sandboxes,
        "verifier_sandboxes": verifier_sandboxes,
        "role_skill_paths": role_skill_paths,
        "verifier_skill_paths": verifier_skill_paths,
        "agent_paths": agent_paths,
        "digest": definition_hasher.hexdigest(),
    }


def validate_provider_routing(
    root: Path,
    raw: bytes,
    policy: dict[str, Any],
    *,
    require_committed: bool = False,
) -> dict[str, Any]:
    """Validate the committed project provider configuration against policy."""

    if len(raw) > 65_536:
        raise PolicyError("project provider routing is too large")
    try:
        config = parse_bounded_json_value(raw, max_bytes=65_536)
    except ContractError as exc:
        raise PolicyError("project provider routing is invalid") from exc
    if not isinstance(config, dict) or set(config) != {
        "version", "providers", "codex_primary_may_bridge_to_claude"
    }:
        raise PolicyError(
            "project provider routing must contain exactly version, providers, "
            "and codex_primary_may_bridge_to_claude"
        )
    providers = config["providers"]
    if (
        config["version"] != 1
        or not isinstance(config["codex_primary_may_bridge_to_claude"], bool)
        or not isinstance(providers, dict)
        or set(providers) != set(PROVIDERS)
    ):
        raise PolicyError(
            "project provider routing version, providers, or bridge flag are invalid"
        )
    for provider, settings in providers.items():
        if (
            not isinstance(settings, dict)
            or not set(settings) <= {"enabled", "models"}
            or "enabled" not in settings
            or not isinstance(settings["enabled"], bool)
        ):
            raise PolicyError(f"project provider routing for {provider} is invalid")
        if "models" in settings:
            models = settings["models"]
            known = _provider_models(policy, provider)
            # Check types before hashing, so malformed entries fail as policy errors.
            if (
                not isinstance(models, list)
                or not models
                or not all(isinstance(model, str) for model in models)
                or len(models) != len(set(models))
                or any(model not in known for model in models)
            ):
                raise PolicyError(
                    f"project provider routing models for {provider} are invalid"
                )
    if require_committed:
        _bind_committed_project_bytes(
            root,
            {PROVIDER_ROUTING.as_posix(): raw},
            subject="project provider routing",
        )
    return config


def load_effective_policy(
    root: Path,
    policy_path: Path,
    discovery_policy_path: Path,
) -> dict[str, Any]:
    """Apply committed project configuration to the accepted base policy.

    Discovery alone never selects an agent for execution. A project agent becomes a
    route candidate only when the project mapping names it for a supported family.
    Model and effort remain owned by the base routing policy. The optional provider
    configuration only narrows which policy candidates are eligible.
    """

    policy = load_policy(policy_path)
    resolved_root = root.resolve()
    effective = deepcopy(policy)
    provider_path = resolved_root / PROVIDER_ROUTING
    if provider_path.is_symlink():
        raise PolicyError("project provider routing is not a regular file")
    if not provider_path.exists() and _committed_in_head(
        resolved_root, PROVIDER_ROUTING.as_posix()
    ):
        # Deleting a committed configuration must not silently restore defaults.
        raise ContractError(
            "project provider routing is committed but missing from the worktree"
        )
    if provider_path.exists():
        try:
            provider_raw = _read_project_regular(
                resolved_root, PROVIDER_ROUTING.as_posix(), max_bytes=65_536
            )
        except PolicyError as exc:
            raise PolicyError("project provider routing is invalid") from exc
        provider_config = validate_provider_routing(
            root, provider_raw, policy, require_committed=True
        )
        effective["provider_routing"] = provider_config
        effective["provider_config_digest"] = content_digest(provider_config)
    mapping_path = resolved_root / PROJECT_AGENT_MAPPING
    if mapping_path.is_symlink():
        raise PolicyError("project agent mapping is not a regular file")
    if not mapping_path.exists() and not mapping_path.is_symlink():
        return effective
    try:
        raw = _read_project_regular(
            resolved_root, PROJECT_AGENT_MAPPING.as_posix(), max_bytes=65_536
        )
    except PolicyError as exc:
        raise PolicyError("project agent mapping is invalid") from exc
    mapping = validate_project_agent_mapping(
        root,
        raw,
        policy_path,
        discovery_policy_path,
        require_committed=True,
    )

    effective["project_agent_roles"] = mapping["roles"]
    effective["project_agent_verifiers"] = mapping["verifiers"]
    effective["project_agent_role_sandboxes"] = mapping["role_sandboxes"]
    effective["project_agent_verifier_sandboxes"] = mapping["verifier_sandboxes"]
    effective["project_agent_role_skill_paths"] = mapping["role_skill_paths"]
    effective["project_agent_verifier_skill_paths"] = mapping[
        "verifier_skill_paths"
    ]
    effective["project_agent_paths"] = mapping["agent_paths"]
    effective["project_claude_agent_roles"] = mapping["claude_roles"]
    effective["project_claude_agent_role_sandboxes"] = mapping[
        "claude_role_sandboxes"
    ]
    effective["project_agent_mapping_digest"] = mapping["digest"]
    return effective


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot load routing policy {path}: {exc}") from exc
    required = {
        "version",
        "status",
        "models",
        "roles",
        "routes",
        "verification",
        "bounds",
        "max_parallel",
    }
    missing = sorted(required - policy.keys())
    if missing:
        raise PolicyError(f"routing policy missing: {', '.join(missing)}")
    if policy["status"] not in {"candidate", "accepted"}:
        raise PolicyError("routing policy status must be candidate or accepted")
    if not isinstance(policy["version"], int) or policy["version"] < 1:
        raise PolicyError("routing policy version must be a positive integer")
    if not isinstance(policy.get("selection_policy"), str) or re.fullmatch(
        r"[a-z0-9_]+", policy["selection_policy"]
    ) is None:
        raise PolicyError("selection_policy must be a typed identifier")
    if not isinstance(policy["models"], dict) or not policy["models"]:
        raise PolicyError("routing policy models must be a non-empty object")
    if any(
        not isinstance(model, str)
        or not isinstance(efforts, list)
        or not efforts
        or not all(isinstance(effort, str) for effort in efforts)
        for model, efforts in policy["models"].items()
    ):
        raise PolicyError("each policy model needs a non-empty effort string array")
    legacy_fallback = policy.get("legacy_fallback")
    if (
        not isinstance(legacy_fallback, dict)
        or set(legacy_fallback) != {"model", "effort"}
    ):
        raise PolicyError("legacy_fallback must contain exactly model and effort")
    _validate_model_effort(
        policy, legacy_fallback.get("model"), legacy_fallback.get("effort")
    )
    if legacy_fallback["model"] != "gpt-5.6-sol":
        raise PolicyError("gpt-5.6-sol must be the sole legacy fallback")
    if not isinstance(policy["max_parallel"], int) or policy["max_parallel"] < 1:
        raise PolicyError("max_parallel must be at least 1")
    verification = policy["verification"]
    if not isinstance(verification, dict):
        raise PolicyError("verification must be an object")
    if verification.get("mode") != "all_meaningful":
        raise PolicyError("initial policy must verify all meaningful results")
    _validate_model_effort(policy, verification.get("model"), verification.get("effort"))
    if "by_risk" in verification:
        _validate_verifier_by_risk(verification["by_risk"], policy["models"], "verification")
    if not isinstance(policy["roles"], dict) or not policy["roles"]:
        raise PolicyError("roles must be a non-empty object")
    if not all(isinstance(role, str) and role for role in policy["roles"].values()):
        raise PolicyError("each role mapping must be a non-empty string")
    if not isinstance(policy["routes"], dict):
        raise PolicyError("routes must be an object")
    families = policy["routes"].get("families", {})
    if not isinstance(families, dict):
        raise PolicyError("route families must be an object")
    if set(families) != set(policy["roles"]):
        raise PolicyError("routing families and role families must match")

    def validate_route(route: Any, label: str) -> None:
        if (
            not isinstance(route, dict)
            or not isinstance(route.get("reason"), str)
            or re.fullmatch(r"[a-z0-9_]+", route["reason"]) is None
        ):
            raise PolicyError(f"{label}: route needs a typed reason code")
        _validate_model_effort(policy, route.get("model"), route.get("effort"))
        alternatives = route.get("alternatives", [])
        if not isinstance(alternatives, list) or len(alternatives) > 8:
            raise PolicyError(f"{label}: alternatives must be a bounded array")
        seen = {(route["model"], route["effort"])}
        for alternative in alternatives:
            if not isinstance(alternative, dict) or set(alternative) != {
                "model", "effort"
            }:
                raise PolicyError(f"{label}: route alternative is invalid")
            _validate_model_effort(
                policy, alternative.get("model"), alternative.get("effort")
            )
            key = (alternative["model"], alternative["effort"])
            if key in seen:
                raise PolicyError(f"{label}: route alternatives must be unique")
            seen.add(key)

    for family, routes in families.items():
        if not isinstance(routes, dict):
            raise PolicyError(f"{family}: routes must be an object")
        if "default" not in routes:
            raise PolicyError(f"{family}: default route is required")
        for name, route in routes.items():
            validate_route(route, f"{family}.{name}")
    for name in ("reconnaissance", "hard_end_to_end"):
        validate_route(policy["routes"].get(name), name)
    bounds = policy["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != BOUND_FIELDS:
        raise PolicyError("bounds must contain exactly the supported fields")
    for field, value in bounds.items():
        minimum = 0 if field in {"max_steers_per_attempt", "max_tool_calls"} else 1
        if not isinstance(value, int) or value < minimum:
            raise PolicyError(f"bounds.{field} must be an integer of at least {minimum}")
    deadlines = policy.get("observation_deadlines")
    if deadlines is None and policy["version"] < 3:
        deadlines = {
            family: bounds["timeout_seconds"] for family in policy["roles"]
        }
        policy = deepcopy(policy)
        policy["observation_deadlines"] = deadlines
    if (
        not isinstance(deadlines, dict)
        or set(deadlines) != set(policy["roles"])
        or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 1
            or value > bounds["timeout_seconds"]
            for value in deadlines.values()
        )
    ):
        raise PolicyError(
            "observation_deadlines must bound every task family within timeout_seconds"
        )
    if policy["version"] >= 5:
        unknown = sorted(set(policy) - POLICY_KEYS)
        if unknown:
            raise PolicyError(f"routing policy has unknown keys: {', '.join(unknown)}")
        if "providers" not in policy or "provider_candidates" not in policy:
            raise PolicyError(
                "routing policy version 5 requires providers and provider_candidates"
            )
    _validate_providers(policy)
    _validate_provider_candidates(policy)
    _validate_escalation(policy)
    _validate_risk_candidates(policy)
    return policy


def _validate_model_effort(policy: dict[str, Any], model: Any, effort: Any) -> None:
    if model not in policy["models"]:
        raise PolicyError(f"policy selected unknown model: {model}")
    if effort not in policy["models"][model]:
        raise PolicyError(f"effort {effort!r} is unsupported for {model}")


def _provider_models(policy: dict[str, Any], provider: str) -> dict[str, Any]:
    if provider == "codex":
        return policy["models"]
    return policy.get("providers", {}).get(provider, {}).get("models", {})


def _provider_roles(policy: dict[str, Any], provider: str) -> dict[str, Any]:
    if provider == "codex":
        return policy["roles"]
    return policy.get("providers", {}).get(provider, {}).get("roles", {})


ADMISSION_POLICY_KEYS = {"client_versions", "max_admissions_per_window"}
CLIENT_VERSION_KEYS = {"minimum", "maximum_exclusive"}
MAX_ADMISSIONS_PER_WINDOW = 10_000


def _validate_admission_settings(provider: str, entry: dict[str, Any]) -> None:
    """The reviewed client range and the run-count backstop for one provider."""

    versions = entry["client_versions"]
    if not isinstance(versions, dict) or set(versions) != CLIENT_VERSION_KEYS:
        raise PolicyError(
            f"providers.{provider}.client_versions must contain exactly minimum "
            "and maximum_exclusive"
        )
    minimum = parse_semver(versions["minimum"])
    maximum = parse_semver(versions["maximum_exclusive"])
    if minimum is None or maximum is None or not minimum < maximum:
        raise PolicyError(
            f"providers.{provider}.client_versions must be an ascending semantic "
            "version range"
        )
    cap = entry["max_admissions_per_window"]
    if cap is not None and (
        not isinstance(cap, int)
        or isinstance(cap, bool)
        or not 1 <= cap <= MAX_ADMISSIONS_PER_WINDOW
    ):
        raise PolicyError(
            f"providers.{provider}.max_admissions_per_window must be a positive "
            "integer or null"
        )


def admission_settings(policy: dict[str, Any], provider: str) -> dict[str, Any]:
    """Return one provider's validated admission settings from a loaded policy."""

    entry = policy.get("providers", {}).get(provider)
    if not isinstance(entry, dict) or not ADMISSION_POLICY_KEYS <= set(entry):
        raise PolicyError(f"routing policy has no admission settings for {provider}")
    _validate_admission_settings(provider, entry)
    return {
        "client_versions": dict(entry["client_versions"]),
        "max_admissions_per_window": entry["max_admissions_per_window"],
    }


def _validate_providers(policy: dict[str, Any]) -> None:
    providers = policy.get("providers", {})
    if not isinstance(providers, dict) or not set(providers) <= set(PROVIDERS):
        raise PolicyError("providers may describe only supported providers")
    if policy["version"] >= 5 and set(providers) != set(PROVIDERS):
        raise PolicyError("routing policy version 5 must describe every provider")
    try:
        if any(provider_for_model(model) != "codex" for model in policy["models"]):
            raise PolicyError("Codex policy models must be GPT models")
        for provider, entry in providers.items():
            if provider == "codex":
                # Codex models and roles stay at the top level of the policy;
                # its provider entry holds only admission settings.
                if not isinstance(entry, dict) or set(entry) != ADMISSION_POLICY_KEYS:
                    raise PolicyError(
                        "providers.codex must contain exactly client_versions and "
                        "max_admissions_per_window"
                    )
                _validate_admission_settings(provider, entry)
                continue
            if not isinstance(entry, dict) or set(entry) != {
                "models", "roles", "verifier", *ADMISSION_POLICY_KEYS
            }:
                raise PolicyError(
                    f"providers.{provider} must contain exactly models, roles, verifier, "
                    "client_versions and max_admissions_per_window"
                )
            _validate_admission_settings(provider, entry)
            models = entry["models"]
            if (
                not isinstance(models, dict)
                or not models
                or any(
                    provider_for_model(model) != provider
                    or not isinstance(efforts, list)
                    or not efforts
                    or not all(isinstance(effort, str) for effort in efforts)
                    or len(efforts) != len(set(efforts))
                    for model, efforts in models.items()
                )
            ):
                raise PolicyError(
                    f"providers.{provider}.models must map its own models to efforts"
                )
            roles = entry["roles"]
            if (
                not isinstance(roles, dict)
                or set(roles) != set(policy["roles"])
                or any(provider_for_role(role) != provider for role in roles.values())
            ):
                raise PolicyError(
                    f"providers.{provider}.roles must name its own role for every family"
                )
            verifier = entry["verifier"]
            if (
                not isinstance(verifier, dict)
                or not {"model", "effort"} <= set(verifier)
                or set(verifier) - {"model", "effort", "by_risk"}
                or verifier["model"] not in models
                or verifier["effort"] not in models[verifier["model"]]
            ):
                raise PolicyError(f"providers.{provider}.verifier is invalid")
            if "by_risk" in verifier:
                _validate_verifier_by_risk(
                    verifier["by_risk"], models, f"providers.{provider}.verifier"
                )
    except ContractError as exc:
        raise PolicyError(f"provider policy is invalid: {exc}") from exc


def _validate_verifier_by_risk(by_risk: Any, models: Any, label: str) -> None:
    """Per-risk verifier settings: a model and effort the provider offers, per band."""

    if not isinstance(by_risk, dict) or not by_risk or not set(by_risk) <= set(RISK_BANDS):
        raise PolicyError(f"{label}.by_risk must map risk bands to a model and effort")
    for band, setting in by_risk.items():
        if (
            not isinstance(setting, dict)
            or set(setting) != {"model", "effort"}
            or setting["model"] not in models
            or setting["effort"] not in models[setting["model"]]
        ):
            raise PolicyError(f"{label}.by_risk.{band} is invalid")


def _validate_provider_candidates(policy: dict[str, Any]) -> None:
    table = policy.get("provider_candidates", {})
    families = policy["routes"]["families"]
    if not isinstance(table, dict) or not set(table) <= {
        *families, *TOP_LEVEL_ROUTE_KEYS
    }:
        raise PolicyError("provider_candidates contains an unknown family or route")
    for name, entry in table.items():
        if name in TOP_LEVEL_ROUTE_KEYS:
            # Reconnaissance always runs as research; hard end-to-end work keeps
            # the task's own family, so every family needs a role.
            role_families = sorted(families) if name == "hard_end_to_end" else [
                "research"
            ]
            _validate_candidate_list(policy, entry, role_families, name)
            continue
        if not isinstance(entry, dict) or not set(entry) <= (
            set(families[name]) & ROUTE_KEYS
        ):
            raise PolicyError(f"provider_candidates.{name} contains an unknown route key")
        for key, candidates in entry.items():
            _validate_candidate_list(policy, candidates, [name], f"{name}.{key}")


def _validate_candidate_list(
    policy: dict[str, Any],
    candidates: Any,
    role_families: list[str],
    label: str,
    table: str = "provider_candidates",
) -> None:
    if not isinstance(candidates, list) or not candidates or len(candidates) > 8:
        raise PolicyError(f"{table}.{label} must be a bounded non-empty array")
    seen: set[tuple[str, ...]] = set()
    for candidate in candidates:
        if candidate == CODEX_POLICY_CANDIDATE:
            provider = "codex"
            key: tuple[str, ...] = ("codex", "codex_policy")
        elif isinstance(candidate, dict) and set(candidate) == {
            "provider", "model", "effort"
        }:
            provider = candidate["provider"]
            if provider not in PROVIDERS or (
                provider != "codex" and provider not in policy.get("providers", {})
            ):
                raise PolicyError(
                    f"{table}.{label} names an unknown provider"
                )
            models = _provider_models(policy, provider)
            if candidate["model"] not in models:
                raise PolicyError(
                    f"{table}.{label} names a model {provider} does not offer"
                )
            if candidate["effort"] not in models[candidate["model"]]:
                raise PolicyError(
                    f"{table}.{label} names an effort the model does not allow"
                )
            key = (provider, candidate["model"], candidate["effort"])
        else:
            raise PolicyError(f"{table}.{label} contains an invalid candidate")
        roles = _provider_roles(policy, provider)
        if any(family not in roles for family in role_families):
            raise PolicyError(
                f"{table}.{label} names a provider without a role"
            )
        if key in seen:
            raise PolicyError(f"{table}.{label} must be unique")
        seen.add(key)


def _cell_route(policy: dict[str, Any], name: str, key: str | None) -> dict[str, Any]:
    """The task-shape policy route a provider-candidate cell belongs to."""

    if key is None:
        return policy["routes"][name]
    return policy["routes"]["families"][name][key]


def _identity(
    candidate: dict[str, Any], cell_route: dict[str, Any]
) -> tuple[str, str, str]:
    """Provider, model and effort of a candidate; `codex_policy` is the cell's own route."""

    if candidate == CODEX_POLICY_CANDIDATE:
        return "codex", cell_route["model"], cell_route["effort"]
    return candidate["provider"], candidate["model"], candidate["effort"]


def _ladder_rungs(policy: dict[str, Any], family: str) -> list[tuple[str, str, str]] | None:
    ladder = policy.get("escalation", {}).get("ladders", {}).get(family)
    if ladder is None:
        return None
    return [(rung["provider"], rung["model"], rung["effort"]) for rung in ladder]


def _validate_escalation(policy: dict[str, Any]) -> None:
    """The escalation block: which failures count, and each family's ladder of strength."""

    if "escalation" not in policy:
        return
    block = policy["escalation"]
    if (
        not isinstance(block, dict)
        or not {"failure_reasons", "ladders"} <= set(block)
        or set(block) - {"failure_reasons", "ladders", "scope"}
    ):
        raise PolicyError("escalation must contain failure_reasons, ladders and optional scope")
    if "scope" in block and (
        policy["version"] < 6 or block["scope"] != "risk_banded_judgment"
    ):
        raise PolicyError("escalation.scope must be risk_banded_judgment in policy v6 or later")
    reasons = block["failure_reasons"]
    if (
        not isinstance(reasons, list)
        or not reasons
        or not all(isinstance(reason, str) and reason in FAILURE_REASONS for reason in reasons)
        or len(reasons) != len(set(reasons))
    ):
        raise PolicyError("escalation.failure_reasons must name known failure reasons")
    ladders = block["ladders"]
    families = policy["routes"]["families"]
    if not isinstance(ladders, dict) or not set(ladders) <= set(families):
        raise PolicyError("escalation.ladders names an unknown family")
    for family, ladder in ladders.items():
        label = f"escalation.ladders.{family}"
        if not isinstance(ladder, list) or not 1 <= len(ladder) <= MAX_LADDER_RUNGS:
            raise PolicyError(f"{label} must be a bounded non-empty array, weakest first")
        seen: set[tuple[str, str, str]] = set()
        for rung in ladder:
            if not isinstance(rung, dict) or set(rung) != {"provider", "model", "effort"}:
                raise PolicyError(f"{label} contains an invalid rung")
            provider = rung["provider"]
            if provider not in PROVIDERS or (
                provider != "codex" and provider not in policy.get("providers", {})
            ):
                raise PolicyError(f"{label} names an unknown provider")
            models = _provider_models(policy, provider)
            if rung["model"] not in models or rung["effort"] not in models[rung["model"]]:
                raise PolicyError(f"{label} names a model or effort {provider} does not offer")
            if family not in _provider_roles(policy, provider):
                raise PolicyError(f"{label} names a provider without a role")
            key = (provider, rung["model"], rung["effort"])
            if key in seen:
                raise PolicyError(f"{label} must be unique")
            seen.add(key)
        # A ladder only means something if every candidate of its family sits on it.
        for key_name, candidates in policy.get("provider_candidates", {}).get(family, {}).items():
            cell = _cell_route(policy, family, key_name)
            for candidate in candidates:
                if _identity(candidate, cell) not in seen:
                    raise PolicyError(
                        f"provider_candidates.{family}.{key_name} has a candidate "
                        f"that is not on the {family} escalation ladder"
                    )


def _validate_risk_candidates(policy: dict[str, Any]) -> None:
    """Per-risk candidate lists; high and critical may never fall below the first choice."""

    if "risk_candidates" not in policy:
        return
    table = policy["risk_candidates"]
    families = policy["routes"]["families"]
    if not isinstance(table, dict) or not set(table) <= {*families, "reconnaissance"}:
        raise PolicyError(
            "risk_candidates contains an unknown family or route "
            "(hard_end_to_end keeps its single route)"
        )
    for name, entry in table.items():
        if name == "reconnaissance":
            cells: dict[str | None, Any] = {None: entry}
        else:
            if not isinstance(entry, dict) or not set(entry) <= (
                set(families[name]) & ROUTE_KEYS
            ):
                raise PolicyError(f"risk_candidates.{name} contains an unknown route key")
            cells = dict(entry)
        for key, bands in cells.items():
            label = name if key is None else f"{name}.{key}"
            if not isinstance(bands, dict) or not bands or not set(bands) <= set(RISK_BANDS):
                raise PolicyError(f"risk_candidates.{label} must map risk bands to candidates")
            family = "research" if name == "reconnaissance" else name
            cell_route = _cell_route(policy, name, key)
            default = _provider_candidate_list(policy, name, key)
            for band, candidates in bands.items():
                _validate_candidate_list(
                    policy, candidates, [family], f"{label}.{band}", "risk_candidates"
                )
                ladder = _ladder_rungs(policy, family)
                if policy["version"] >= 6 and ladder is not None and any(
                    _identity(candidate, cell_route) not in ladder for candidate in candidates
                ):
                    raise PolicyError(f"risk_candidates.{label}.{band} is off the {family} ladder")
                if band not in HIGH_RISK_BANDS:
                    continue
                ladder = _ladder_rungs(policy, family)
                if ladder is None:
                    raise PolicyError(
                        f"risk_candidates.{label}.{band} needs an escalation ladder for "
                        f"{family} to show it never routes below the first choice"
                    )
                floor_identity = _identity(default[0], cell_route)
                if floor_identity not in ladder:
                    raise PolicyError(
                        f"the first choice of {label} is not on the {family} escalation ladder"
                    )
                floor = ladder.index(floor_identity)
                for candidate in candidates:
                    identity = _identity(candidate, cell_route)
                    if identity not in ladder or ladder.index(identity) < floor:
                        raise PolicyError(
                            f"risk_candidates.{label}.{band} routes below the first choice "
                            f"of {label} or off the {family} escalation ladder"
                        )


def _candidate(
    task: dict[str, Any],
    policy: dict[str, Any],
    trusted_routing_evidence_refs: frozenset[str],
) -> tuple[dict[str, Any], str, str]:
    family = task["family"]
    complexity = task["complexity"]
    operation = task["operation"]
    ambiguity = task["ambiguity"]
    context_breadth = task["context_breadth"]
    family_routes = policy["routes"]["families"].get(family)
    if family_routes is None:
        raise ContractError(f"unsupported family: {family}")
    if ambiguity == "unknown" or context_breadth == "unknown":
        return policy["routes"]["reconnaissance"], "reconnaissance", "research"
    hard_end_to_end = (
        operation == "end_to_end"
        and complexity == "extreme"
        and ambiguity == "high"
        and context_breadth == "cross_system"
        and bool(task["routing_evidence_refs"])
        and set(task["routing_evidence_refs"]) <= trusted_routing_evidence_refs
    )
    if hard_end_to_end:
        return policy["routes"]["hard_end_to_end"], "execution", family
    if family == "security":
        return family_routes["default"], "execution", family
    if family == "planning":
        name = (
            "synthesis"
            if (
                policy["version"] >= 6
                and task["risk"] in {"low", "medium"}
                and task["acceptance_strength"] == "independent_judgment"
                and task["risk"] in policy.get("risk_candidates", {}).get(
                    "planning", {}
                ).get("synthesis", {})
            )
            or complexity in {"high", "extreme"}
            or ambiguity == "high"
            or context_breadth == "cross_system"
            or operation in {"synthesis", "end_to_end"}
            else "default"
        )
        return family_routes[name], "execution", family
    strong_acceptance = _strong_acceptance(task)
    if family == "verification":
        bounded_verification = (
            ambiguity in {"known", "partial"}
            and context_breadth in {"narrow", "repository"}
            and strong_acceptance
            and complexity != "extreme"
        )
        name = "bounded" if bounded_verification else "default"
        return family_routes[name], "execution", family
    bounded = (
        operation in {"mechanical", "implementation"}
        and ambiguity in {"known", "partial"}
        and context_breadth in {"narrow", "repository"}
        and strong_acceptance
        and complexity != "extreme"
    )
    if bounded:
        return family_routes["bounded"], "execution", family
    if operation in {"exploration", "diagnosis", "review"}:
        name = "judgment" if complexity in {"high", "extreme"} else "exploratory"
        return family_routes[name], "execution", family
    if operation in {"synthesis", "end_to_end"} or context_breadth == "cross_system":
        return family_routes["synthesis"], "execution", family
    if ambiguity == "high":
        name = "judgment" if complexity in {"high", "extreme"} else "exploratory"
        return family_routes[name], "execution", family
    return family_routes["default"], "execution", family


def _selection_metadata(route: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    selected = {"model": route["model"], "effort": route["effort"]}
    return {
        "policy_version": policy["version"],
        "strategy": policy["selection_policy"],
        "selected": selected,
        "eligible": [selected, *deepcopy(route.get("alternatives", []))],
        "selection_probability_micros": 1_000_000,
    }


def _route_key(
    policy: dict[str, Any], family: str, route: dict[str, Any]
) -> tuple[str, str | None]:
    """Name the policy entry `_candidate` chose without repeating its logic."""

    for name in TOP_LEVEL_ROUTE_KEYS:
        if route is policy["routes"][name]:
            return name, None
    for key, value in policy["routes"]["families"][family].items():
        if route is value:
            return family, key
    raise ContractError("routed policy entry is unavailable")


def _provider_candidate_list(
    policy: dict[str, Any], name: str, key: str | None
) -> list[dict[str, Any]]:
    table = policy.get("provider_candidates", {})
    entry = table.get(name) if key is None else table.get(name, {}).get(key)
    return entry or [CODEX_POLICY_CANDIDATE]


def _banded_candidate_list(
    policy: dict[str, Any], name: str, key: str | None, risk: str
) -> tuple[list[dict[str, Any]], str | None]:
    """The task's risk band list when the policy gives one, else today's list."""

    table = policy.get("risk_candidates", {})
    bands = table.get(name) if key is None else table.get(name, {}).get(key)
    banded = bands.get(risk) if bands else None
    if banded:
        return banded, risk
    return _provider_candidate_list(policy, name, key), None


def _strong_acceptance(task: dict[str, Any]) -> bool:
    return task["acceptance_strength"] == "executable" or (
        task["acceptance_strength"] == "deterministic"
        and task["deterministic_validation"]
    )


def _provider_settings(policy: dict[str, Any]) -> tuple[dict[str, Any], str]:
    config = policy.get("provider_routing")
    if config is None:
        return DEFAULT_PROVIDER_ROUTING, DEFAULT_PROVIDER_CONFIG_DIGEST
    return config, policy["provider_config_digest"]


def provider_model_allowed(policy: dict[str, Any], provider: str, model: str) -> bool:
    """Return whether committed project configuration permits this provider model."""

    config, _ = _provider_settings(policy)
    settings = config["providers"].get(provider)
    if settings is None or not settings["enabled"]:
        return False
    return "models" not in settings or model in settings["models"]


def _provider_admitted(
    policy: dict[str, Any], primary_provider: str, provider: str, model: str
) -> bool:
    if not provider_model_allowed(policy, provider, model):
        return False
    config, _ = _provider_settings(policy)
    # Until the owner settles the Codex sandbox question, a Codex primary may
    # reach Claude only when the project explicitly allows the bridge.
    return not (
        provider == "claude"
        and primary_provider == "codex"
        and not config["codex_primary_may_bridge_to_claude"]
    )


def _dispatch(primary_provider: str, provider: str) -> str:
    """How the primary runs a child of `provider`: always through the bridge.

    A native Codex child shares the Codex primary's session, and with it the
    owner's command rule that runs the primary-host commands outside the
    sandbox. Every Codex child therefore goes through the bridge (`codex exec`,
    read-only, `--ignore-rules`), from either primary, so no child can use the
    rule. A Codex route that needs write access is ineligible until write
    containment exists.

    A Claude child also goes through the bridge (`claude -p`, read-only tools),
    from either primary (owner decision for 0.3.0): only a separate process can be
    watched, cut off on silence or deadline and cleaned up by the project host
    whatever the primary is doing, and only its reads are contained. A native
    subagent can be observed but not stopped from outside its session.
    """

    return "bridge"


def _claude_agent(policy: dict[str, Any], family: str) -> dict[str, str] | None:
    """Resolve the Claude role or mapped Claude agent; write tools are ineligible."""

    role = _provider_roles(policy, "claude").get(family)
    if role is None:
        return None
    agent_type = policy.get("project_claude_agent_roles", {}).get(family, role)
    sandbox = policy.get("project_claude_agent_role_sandboxes", {}).get(
        family, "read-only"
    )
    if sandbox != "read-only":
        return None
    return {
        "role": role,
        "agent_type": agent_type,
        "agent_path": f".claude/agents/{agent_type}.md",
        "sandbox_mode": "read-only",
    }


def _codex_project_agent(
    policy: dict[str, Any], task: dict[str, Any], routed_family: str, role: str
) -> dict[str, Any]:
    """Resolve the explicitly mapped Codex agent fields for a Codex route."""

    if policy.get("project_agent_mapping_digest") is None:
        return {}
    result: dict[str, Any] = {}
    verification_for_family = task.get("verification_for_family")
    if task["family"] == "verification" and verification_for_family is not None:
        result["agent_type"] = policy.get("project_agent_verifiers", {}).get(
            verification_for_family,
            policy["project_agent_roles"].get("verification", role),
        )
        result["sandbox_mode"] = policy.get(
            "project_agent_verifier_sandboxes", {}
        ).get(
            verification_for_family,
            policy.get("project_agent_role_sandboxes", {}).get(
                "verification", "read-only"
            ),
        )
        result["skill_paths"] = policy.get(
            "project_agent_verifier_skill_paths", {}
        ).get(
            verification_for_family,
            policy.get("project_agent_role_skill_paths", {}).get(
                "verification", []
            ),
        )
    else:
        result["agent_type"] = policy["project_agent_roles"].get(routed_family, role)
        result["sandbox_mode"] = policy.get(
            "project_agent_role_sandboxes", {}
        ).get(routed_family, "read-only")
        result["skill_paths"] = policy.get(
            "project_agent_role_skill_paths", {}
        ).get(routed_family, [])
    agent_path = policy.get("project_agent_paths", {}).get(result["agent_type"])
    if agent_path is not None:
        result["agent_path"] = agent_path
    return result


def verifier_setting(
    policy: dict[str, Any], provider: str, risk: str
) -> tuple[dict[str, str], bool]:
    """The verifier model and effort for the worker's risk, and whether a per-risk entry applied.

    Absent a per-risk entry the provider's single verifier setting applies, as before.
    """

    source = (
        policy["verification"]
        if provider == "codex"
        else policy["providers"][provider]["verifier"]
    )
    banded = source.get("by_risk", {}).get(risk)
    chosen = banded if banded is not None else source
    return {"model": chosen["model"], "effort": chosen["effort"]}, banded is not None


def _codex_verifier(
    policy: dict[str, Any],
    routed_family: str,
    *,
    use_legacy_fallback: bool,
    risk: str,
) -> dict[str, Any]:
    verification = policy["verification"]
    setting, banded = verifier_setting(policy, "codex", risk)
    source = policy["legacy_fallback"] if use_legacy_fallback else setting
    banded = banded and not use_legacy_fallback
    verifier: dict[str, Any] = {
        "role": verification["role"],
        "model": source["model"],
        "effort": source["effort"],
        "sandbox_mode": "read-only",
    }
    if banded:
        verifier["risk_band"] = risk
    verifier_agent_type = policy.get("project_agent_verifiers", {}).get(
        routed_family
    )
    if verifier_agent_type is None:
        verifier_agent_type = policy.get("project_agent_roles", {}).get("verification")
    if verifier_agent_type is not None:
        verifier["agent_type"] = verifier_agent_type
        verifier_agent_path = policy.get("project_agent_paths", {}).get(
            verifier_agent_type
        )
        if verifier_agent_path is not None:
            verifier["agent_path"] = verifier_agent_path
        verifier["sandbox_mode"] = policy.get(
            "project_agent_verifier_sandboxes", {}
        ).get(
            routed_family,
            policy.get("project_agent_role_sandboxes", {}).get(
                "verification", "read-only"
            ),
        )
        verifier["skill_paths"] = policy.get(
            "project_agent_verifier_skill_paths", {}
        ).get(
            routed_family,
            policy.get("project_agent_role_skill_paths", {}).get(
                "verification", []
            ),
        )
    return verifier


def _bridged_writable(agent: dict[str, Any]) -> str:
    """Why a mapped Codex agent was skipped: Codex children are bridged read-only."""

    return (
        f"the mapped Codex agent {agent.get('agent_type', 'agent')} is "
        f"{agent['sandbox_mode']}, and Codex children are bridged read-only in this "
        "release, so it is not used for Codex routes"
    )


def _select_verifier(
    policy: dict[str, Any],
    routed_family: str,
    *,
    worker_provider: str,
    primary_provider: str,
    use_legacy_fallback: bool,
    risk: str,
    independent: bool = False,
    availability: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prefer the other vendor, then the worker's own; judgment cells mark that fallback not independent.

    Its model and effort follow the worker's risk where the policy gives per-risk entries.
    """

    order = ("codex", "claude") if worker_provider == "claude" else ("claude", "codex")
    skipped: str | None = None
    for provider in order:
        if availability is not None and availability["providers"][provider]["state"] == "exhausted":
            availability["candidates"].append({"phase": "verifier", "provider": provider, "skip": "usage"})
            continue
        dispatch = _dispatch(primary_provider, provider)
        if provider == "codex":
            verifier = _codex_verifier(
                policy, routed_family, use_legacy_fallback=use_legacy_fallback, risk=risk
            )
            if dispatch == "bridge" and verifier["sandbox_mode"] != "read-only":
                skipped = _bridged_writable(verifier)
                if availability is not None:
                    availability["candidates"].append({"phase": "verifier", "provider": provider,
                                                       "skip": "containment", "detail": skipped})
                continue
        else:
            claude = policy.get("providers", {}).get("claude")
            agent = _claude_agent(policy, "verification")
            if claude is None or agent is None:
                if availability is not None:
                    availability["candidates"].append({"phase": "verifier", "provider": provider,
                                                       "skip": "configuration"})
                continue
            setting, banded = verifier_setting(policy, "claude", risk)
            verifier = {
                "role": agent["role"],
                "model": setting["model"],
                "effort": setting["effort"],
                "sandbox_mode": "read-only",
                "agent_type": agent["agent_type"],
                "agent_path": agent["agent_path"],
            }
            if banded:
                verifier["risk_band"] = risk
        if not _provider_admitted(
            policy, primary_provider, provider, verifier["model"]
        ):
            if availability is not None:
                availability["candidates"].append({"phase": "verifier", "provider": provider,
                                                   "skip": "configuration"})
            continue
        verifier["provider"] = provider
        verifier["dispatch"] = dispatch
        if provider == worker_provider:
            # Same-provider fallback is kept (owner decision 17); the record says so.
            verifier["independent"] = False
        return verifier
    if availability is not None and any(v["state"] == "exhausted" for v in availability["providers"].values()):
        raise _AvailabilityRefusal("verifier", availability)
    message = "no verifier candidate is eligible for this route"
    raise ContractError(f"{message}: {skipped}" if skipped else message)


def _escalate(
    policy: dict[str, Any],
    task: dict[str, Any],
    routed_family: str,
    picked: tuple[str, dict[str, Any], dict[str, Any] | None],
    first_eligible: Any,
    *,
    judgment_cell: bool = False,
) -> tuple[tuple[str, dict[str, Any], dict[str, Any] | None], dict[str, Any] | None]:
    """Move a retry up its family's ladder after a failed check, once, then to the strongest.

    Attempt 2 after a qualifying failure goes to the next admitted rung above the pick.
    Judgment cells require independent verification evidence; legacy judgment keeps its
    first choice. Attempt 3 goes to the strongest admitted rung and is reported.
    The policy opts in with an `escalation` block; without one, or without a ladder for
    the family, the pick stands and nothing is recorded.
    """

    block = policy.get("escalation")
    ladder = _ladder_rungs(policy, routed_family) if block is not None else None
    failed = task["failed_attempts"]
    scoped = block is not None and block.get("scope") == "risk_banded_judgment"
    if (
        ladder is None
        or failed == 0
        or (scoped and not judgment_cell)
        or (not scoped and not any(
            event["reason"] in block["failure_reasons"] for event in task["failure_events"]
        ))
    ):
        return picked, None
    provider, model_effort, _ = picked
    current = (provider, model_effort["model"], model_effort["effort"])
    record: dict[str, Any] = {
        "attempt": failed + 1,
        "from": {"provider": current[0], "model": current[1], "effort": current[2]},
    }
    confirmed = any(
        event["attempt"] == failed
        and event["reason"] in block["failure_reasons"]
        and any(VERIFIED_FAILURE_REFERENCE.fullmatch(ref) for ref in event["evidence_refs"])
        for event in task["failure_events"]
    )
    if (scoped and not confirmed) or (
        failed == 1 and not _strong_acceptance(task) and not judgment_cell
    ):
        record["step"] = "held_judgment"
        record["to"] = deepcopy(record["from"])
        return picked, record
    if current not in ladder:
        record["step"] = "off_ladder"
        record["to"] = deepcopy(record["from"])
        return picked, record
    above = [
        {"provider": p, "model": m, "effort": e}
        for p, m, e in ladder[ladder.index(current) + 1:]
    ]
    # The next rung up is the nearest admitted one; the strongest is the highest.
    for rung in (above if failed == 1 else list(reversed(above))):
        found, _ = first_eligible([rung])
        if found is not None:
            record["step"] = "next" if failed == 1 else "strongest"
            record["to"] = deepcopy(rung)
            return found, record
    record["step"] = "none_higher"
    record["to"] = deepcopy(record["from"])
    return picked, record


def _route_task(
    task_data: Any,
    policy: dict[str, Any],
    *,
    trusted_routing_evidence_refs: frozenset[str] = frozenset(),
    use_legacy_fallback: bool = False,
    primary_provider: str = "codex",
    provider_snapshot: ProviderStateSnapshot | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(use_legacy_fallback, bool):
        raise ContractError("legacy fallback selection must be boolean")
    if primary_provider not in PROVIDERS:
        raise ContractError("primary provider must be codex or claude")
    task = validate_task(task_data, allow_unadjudicated_failures=policy["version"] >= 6)
    candidate, stage, routed_family = _candidate(
        task, policy, trusted_routing_evidence_refs
    )
    if provider_snapshot is not None:
        if type(provider_snapshot) is not ProviderStateSnapshot or provider_snapshot.project_id != project_id:
            raise ContractError("provider snapshot context does not match route")
        if set(provider_snapshot.providers) != set(PROVIDERS):
            raise ContractError("provider snapshot must cover both providers")
        availability = provider_snapshot.audit()
    else:
        availability = {"status": "not_checked", "providers": {
            provider: {"state": "not_checked", "known": False} for provider in PROVIDERS}}
    availability.update(candidates=[], reroute_count=0, usage_switch=False)
    _, provider_config_digest = _provider_settings(policy)
    codex_role = policy["roles"][routed_family]
    codex_agent = _codex_project_agent(policy, task, routed_family, codex_role)

    # The task-shape route stays the Codex baseline; provider candidates only
    # reorder providers, and the first eligible one wins. None eligible denies.
    picked: tuple[str, dict[str, Any], dict[str, Any] | None] | None = None
    verifier_independent: bool | None = None
    verification_for_provider = task.get("verification_for_provider")
    if verification_for_provider is not None:
        # A verification task for known work follows the same rule as the worker
        # route's advertised verifier, so both name the same provider and model.
        rule = _select_verifier(
            policy,
            task.get("verification_for_family", "verification"),
            worker_provider=verification_for_provider,
            primary_provider=primary_provider,
            use_legacy_fallback=use_legacy_fallback,
            risk=task["risk"],
            availability=availability,
            independent=(
                policy.get("escalation", {}).get("scope") == "risk_banded_judgment"
                and task["risk"] in {"low", "medium"}
                and task.get("verification_for_family") in {"coding", "research", "planning"}
            ),
        )
        verifier_independent = rule.get("independent")
        rule_agent = None
        if rule["provider"] == "claude":
            rule_agent = {
                field: rule[field]
                for field in ("role", "agent_type", "agent_path", "sandbox_mode")
            }
        picked = (
            rule["provider"],
            {"model": rule["model"], "effort": rule["effort"]},
            rule_agent,
        )
    risk_band: str | None = None
    candidate_entries: list[dict[str, Any]] = []
    if picked is None:
        candidate_entries, risk_band = _banded_candidate_list(
            policy, *_route_key(policy, routed_family, candidate), task["risk"]
        )
    route_name, route_key = _route_key(policy, routed_family, candidate)
    judgment_scope = (
        policy.get("escalation", {}).get("scope") == "risk_banded_judgment"
        and routed_family in {"coding", "research", "planning"}
        and route_name == routed_family
        and route_key in {"exploratory", "judgment", "synthesis"}
        and task["risk"] in {"low", "medium"}
    )
    judgment_cell = judgment_scope and risk_band is not None

    def first_eligible(
        entries: list[dict[str, Any]],
    ) -> tuple[tuple[str, dict[str, Any], dict[str, Any] | None] | None, str | None]:
        skipped: str | None = None
        for entry in entries:
            provider = entry["provider"]
            considered = {"phase": "worker", "provider": provider, "model": entry.get("model")}
            availability["candidates"].append(considered)
            if availability["providers"][provider]["state"] == "exhausted":
                considered["skip"] = "usage"
                availability["usage_switch"] = True
                continue
            source = candidate if entry == CODEX_POLICY_CANDIDATE else entry
            model_effort = {"model": source["model"], "effort": source["effort"]}
            if not _provider_admitted(
                policy, primary_provider, provider, model_effort["model"]
            ):
                considered["skip"] = "configuration"
                continue
            agent: dict[str, Any] | None = None
            if provider == "codex":
                if (
                    _dispatch(primary_provider, provider) == "bridge"
                    and codex_agent.get("sandbox_mode", "read-only") != "read-only"
                ):
                    skipped = _bridged_writable(codex_agent)
                    considered["skip"] = "containment"
                    continue
            else:
                agent = _claude_agent(policy, routed_family)
                if agent is None:
                    considered["skip"] = "agent_unavailable"
                    continue
            considered["selected"] = True
            return (provider, model_effort, agent), skipped
        return None, skipped

    if picked is None:
        picked, skipped = first_eligible(candidate_entries)
    else:
        skipped = None
    if picked is None:
        if any(v["state"] == "exhausted" for v in availability["providers"].values()):
            raise _AvailabilityRefusal("worker", availability)
        message = "no provider candidate is eligible for this route"
        raise ContractError(f"{message}: {skipped}" if skipped else message)
    escalation: dict[str, Any] | None = None
    if verification_for_provider is None:
        picked, escalation = _escalate(
            policy, task, routed_family, picked, first_eligible,
            judgment_cell=judgment_cell,
        )
    provider, primary, claude_agent = picked
    selected = primary
    reason = candidate["reason"]
    fallback: dict[str, Any] | None = None
    if use_legacy_fallback:
        if provider != "codex":
            raise ContractError("legacy fallback applies only to Codex routes")
        if verification_for_provider is not None:
            # The verifier rule already applied the fallback model, exactly as the
            # worker route advertises it; record the usual verifier as the source.
            selected = deepcopy(primary)
            primary = {
                "model": policy["verification"]["model"],
                "effort": policy["verification"]["effort"],
            }
        else:
            selected = deepcopy(policy["legacy_fallback"])
        if selected == primary:
            raise ContractError("selected route is already the legacy fallback")
        if not _provider_admitted(
            policy, primary_provider, "codex", selected["model"]
        ):
            raise ContractError(
                "legacy fallback is not allowed by the project provider configuration"
            )
        reason = "selected_model_unavailable_fallback"
        fallback = {
            "trigger": "host_model_unavailable",
            "from": primary,
            "to": deepcopy(selected),
        }
    model = selected["model"]
    effort = selected["effort"]
    if provider == "codex":
        _validate_model_effort(policy, model, effort)

    can_parallelize = task["parallel_safe"] and task["independent_parts"] > 1
    max_parallel = min(task["independent_parts"], policy["max_parallel"])
    topology = "parallel" if can_parallelize else "single"
    if topology == "single":
        max_parallel = 1

    verification_required = task["meaningful"] and routed_family != "verification"
    verifier: dict[str, Any] | None = None
    if verification_required:
        verifier = _select_verifier(
            policy,
            routed_family,
            worker_provider=provider,
            primary_provider=primary_provider,
            use_legacy_fallback=use_legacy_fallback,
            risk=task["risk"],
            availability=availability,
            independent=(
                policy.get("escalation", {}).get("scope") == "risk_banded_judgment"
                and task["risk"] in {"low", "medium"}
                and routed_family in {"coding", "research", "planning"}
            ),
        )

    route_bounds = dict(policy["bounds"])
    route_bounds["timeout_seconds"] = policy["observation_deadlines"][routed_family]
    selection = _selection_metadata(candidate, policy)
    if (
        policy["version"] == 6
        and policy.get("escalation", {}).get("scope") == "risk_banded_judgment"
        and not judgment_scope
    ):
        # Unopted cells still execute the v5 selector, including its audit identity.
        selection["policy_version"] = 5
    if primary != {"model": candidate["model"], "effort": candidate["effort"]}:
        selection["eligible"] = [deepcopy(primary)]
        if provider == "codex":
            selection["eligible"].extend(
                item for item in deepcopy(candidate.get("alternatives", []))
                if item != primary
            )
    selection["selected"] = deepcopy(selected)
    if use_legacy_fallback:
        selection["fallback_from"] = deepcopy(primary)
        if selected not in selection["eligible"]:
            selection["eligible"].append(deepcopy(selected))
    availability["worker"] = provider if verification_for_provider is None else verification_for_provider
    availability["verifier"] = verifier["provider"] if verifier is not None else (
        provider if verification_for_provider is not None else None)
    result = {
        "availability": availability,
        "task_id": task["id"],
        "role": codex_role,
        "model": model,
        "effort": effort,
        "stage": stage,
        "routed_family": routed_family,
        "topology": topology,
        "max_parallel": max_parallel,
        "verification_required": verification_required,
        "verifier": verifier,
        "bounds": route_bounds,
        "observation_deadline_seconds": route_bounds["timeout_seconds"],
        "deadline_semantics": "host_observation_only",
        "reason": reason,
        "selection": selection,
        "sandbox_mode": "read-only",
        "provider": provider,
        "dispatch": _dispatch(primary_provider, provider),
        "provider_config_digest": provider_config_digest,
    }
    review_fields = {key: task[key] for key in (
        "review_phase", "parent_review_id", "expanded_scope"
    ) if key in task}
    if risk_band is not None or escalation is not None or judgment_scope or review_fields:
        basis: dict[str, Any] = {"risk": task["risk"]}
        if risk_band is not None:
            basis["risk_band"] = risk_band
        if escalation is not None:
            basis["escalation"] = escalation
        if judgment_scope or review_fields:
            basis.update({key: deepcopy(task[key]) for key in (
                "family", "operation", "acceptance_strength", "context_breadth",
                "routing_evidence_refs", "failed_attempts", "failure_events",
            )})
            basis.update(review_fields)
            basis["requested_setting"] = {"provider": provider, "model": model, "effort": effort}
            # Routing requests execution; it cannot attest an observation or a verdict.
            basis["observed_setting"] = None
            basis["verdict_axes"] = dict(
                admission_state=None, execution_state=None, semantic_verdict=None
            )
            basis["usage"] = None
        if judgment_scope and risk_band is None:
            basis["risk_cell_fallback"] = "provider_candidates"
        result["routing_basis"] = basis
    if fallback is not None:
        fallback["verifier_substituted"] = verification_required
        result["fallback"] = fallback
    if verifier_independent is False:
        result["independent"] = False
    mapping_digest = policy.get("project_agent_mapping_digest")
    if mapping_digest is not None:
        result["project_agent_mapping_digest"] = mapping_digest
    if claude_agent is not None:
        result.update(claude_agent)
        if mapping_digest is not None:
            result["skill_paths"] = []
    else:
        result.update(codex_agent)
    return result


def route_task(
    task_data: Any,
    policy: dict[str, Any],
    *,
    trusted_routing_evidence_refs: frozenset[str] = frozenset(),
    primary_provider: str = "codex",
    provider_snapshot: ProviderStateSnapshot | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Select the primary policy route; fallback requires trusted lifecycle evidence."""

    return _route_with_availability(
        task_data,
        policy,
        trusted_routing_evidence_refs=trusted_routing_evidence_refs,
        primary_provider=primary_provider,
        provider_snapshot=provider_snapshot,
        project_id=project_id,
    )


def _route_task_legacy_fallback(
    task_data: Any,
    policy: dict[str, Any],
    *,
    trusted_routing_evidence_refs: frozenset[str] = frozenset(),
    primary_provider: str = "codex",
    provider_snapshot: ProviderStateSnapshot | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Resolve the private fallback after the ledger authorizes host unavailability."""

    return _route_with_availability(
        task_data,
        policy,
        trusted_routing_evidence_refs=trusted_routing_evidence_refs,
        use_legacy_fallback=True,
        primary_provider=primary_provider,
        provider_snapshot=provider_snapshot,
        project_id=project_id,
    )


class _AvailabilityRefusal(Exception):
    def __init__(self, phase, availability):
        self.phase, self.availability = phase, availability


def _route_with_availability(task_data, policy, **kwargs):
    try:
        return _route_task(task_data, policy, **kwargs)
    except _AvailabilityRefusal as refusal:
        audit = refusal.availability
        both = all(value["state"] == "exhausted" for value in audit["providers"].values())
        return {"status": "unsatisfied", "ready": False,
                "reason_code": "no_provider_allowance" if both else "no_eligible_provider",
                "cause": "provider_exhausted", "task_id": task_data["id"], "phase": refusal.phase,
                "evaluated_at_ms": audit.get("evaluated_at_ms"), "snapshot_ref": audit.get("snapshot_ref"),
                "providers": {p: {**value, "cause": "usage" if value["state"] == "exhausted" else "configuration"}
                              for p, value in audit["providers"].items()}, "availability": audit}


def open_routed_task(task, policy, *, project_id, snapshot, open_route, primary_provider="codex",
                     trusted_routing_evidence_refs=frozenset(), resolve_route=None):
    """One logical open: fresh advisory routing and at most one exhaustion recovery.

    The callback must perform normal authoritative admission. No other denial
    grants a retry and no route grants permission to launch.
    """
    from .providers import AdmissionDenied
    attempts = []
    for retry in range(2):
        current = snapshot()
        route = (resolve_route(current) if resolve_route is not None else
                 route_task(task, policy, project_id=project_id, provider_snapshot=current,
                            primary_provider=primary_provider,
                            trusted_routing_evidence_refs=trusted_routing_evidence_refs))
        route["availability"].update(reroute_count=retry, open_attempts=deepcopy(attempts))
        if retry:
            route["availability"]["reroute_trigger"] = "provider_exhausted"
        if route.get("status") == "unsatisfied":
            return {"ready": False, "reason_code": route["reason_code"], "route": route}
        reference = content_digest({"task_id": task["id"], "provider": route["provider"],
                                    "snapshot_ref": route["availability"].get("snapshot_ref"), "attempt": retry + 1})
        attempt = {"provider": route["provider"], "reason_code": "admitted", "open_ref": reference,
                   "snapshot_ref": route["availability"].get("snapshot_ref")}
        route["availability"].update(open_attempt_count=retry + 1,
            original_open_ref=attempts[0]["open_ref"] if attempts else reference,
            replacement_open_ref=reference if retry else None,
            open_attempts=[*deepcopy(attempts), attempt])
        try:
            opened = open_route(route)
        except AdmissionDenied as error:
            attempts.append({**attempt, "reason_code": error.reason_code,
                             **({"denial_reference": error.open_reference}
                                if hasattr(error, "open_reference") else {})})
            if error.reason_code == "provider_exhausted" and retry == 0:
                continue
            route["availability"]["open_attempts"] = attempts
            return {"ready": False, "reason_code": error.reason_code, "route": route}
        return {"ready": True, "route": route, "open": opened}
