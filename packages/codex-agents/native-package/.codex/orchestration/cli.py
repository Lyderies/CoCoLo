#!/usr/bin/env python3
"""Command-line entry point for deterministic native orchestration support."""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ORCHESTRATION_ROOT = Path(__file__).resolve().parent
# The project this CLI serves. A protected runtime copy (the one the owner's Codex
# rule runs) replaces it below with the project its binding names.
BUNDLE_ROOT = ORCHESTRATION_ROOT.parent.parent
# Present only in an installed project or its protected copy; the installer
# writes it next to this file.
INSTALL_MANIFEST = ORCHESTRATION_ROOT / "install-manifest.json"
# Present only in a protected runtime copy.
RUNTIME_BINDING = ORCHESTRATION_ROOT / "runtime-binding.json"


def _copy_holds_only_its_runtime() -> bool:
    """Whether a protected copy's tree is exactly the files its manifest lists.

    It runs before this directory goes on the import path, with only modules
    already loaded, so an extra module (a top-level `socket.py`, a package,
    bytecode, an extension module) is refused before anything could import it.
    """

    try:
        with open(INSTALL_MANIFEST, "rb") as handle:
            listed = json.loads(handle.read(1_048_577))["files"]
        expected = {
            name.removeprefix(".codex/orchestration/") for name in listed
        } | {INSTALL_MANIFEST.name, RUNTIME_BINDING.name}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    directories = {
        "/".join(item.split("/")[:index])
        for item in expected for index in range(1, item.count("/") + 1)
    }
    for current, subdirectories, names in os.walk(ORCHESTRATION_ROOT, followlinks=False):
        base = os.path.relpath(current, ORCHESTRATION_ROOT)
        for name in [*subdirectories, *names]:
            relative = name if base == "." else f"{base}/{name}"
            path = os.path.join(current, name)
            if os.path.islink(path) or relative not in (
                directories if name in subdirectories else expected
            ):
                return False
    return True


if os.path.lexists(RUNTIME_BINDING) and not _copy_holds_only_its_runtime():
    print(json.dumps({
        "ready": False,
        "error": "the protected runtime copy holds files it should not; ask the owner "
        "to run ./setup.sh (Codex commands step)",
    }, indent=2, sort_keys=True))
    raise SystemExit(2)
# Appended, never first: run isolated (-I), the script's folder is not on the path, so a
# module planted beside this file (a `socket.py`) can never shadow the standard library,
# in the project's copy (which the hooks' collector runs) as in the protected one.
sys.path.append(str(ORCHESTRATION_ROOT))

from codex_agents.bridge import BridgeRefused  # noqa: E402
from codex_agents.bridge import dispatch_preflight  # noqa: E402
from codex_agents.catalog import CatalogError, discover, load_discovery_policy  # noqa: E402
from codex_agents.contracts import (  # noqa: E402
    ContractError,
    ResultContractError,
    model_selection_slice,
    result_preflight,
    routing_task_slice,
    validate_assignment,
    validate_assignment_shape,
    validate_result,
    validate_task,
)
from codex_agents.evaluation import evaluate, load_jsonl  # noqa: E402
from codex_agents.learning_adapters import (  # noqa: E402
    load_client_capabilities,
    normalize_app_server,
)
from codex_agents.learning_contracts import (  # noqa: E402
    canonical_json,
    content_digest,
    keyed_digest,
    load_released_candidate_digests,
    parse_bounded_json,
    parse_bounded_json_value,
    provider_for_model,
    validate_learning_record,
)
from codex_agents.learning_engine import (  # noqa: E402
    CandidateLifecycle,
    OwnerGrantAuthority,
    analyze_outcomes,
    apply_candidate_to_route,
    build_feedback,
    build_evaluation_trial,
    build_canary_report,
    calibrate_grader,
    compare_candidate,
    create_evaluation_assignment,
    generate_candidate,
    installed_execution_configuration,
    mine_evaluation_case,
    review_evaluation_case,
    run_operational_check_suite,
    runtime_fixture_digest,
    validate_runtime_candidate,
)
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient  # noqa: E402
from codex_agents.collector_paths import UNMOVED_STATE, CollectorPaths  # noqa: E402
from codex_agents.project_usage_report import DEFAULT_ROWS as USAGE_REPORT_ROWS  # noqa: E402
from codex_agents.project_usage_report import UsageReportError, usage_report  # noqa: E402
from codex_agents.client_runtime import (  # noqa: E402
    RuntimeBillingProbeError, RuntimeObservationError, RuntimeOpenError, RuntimeOrchestrationError, RuntimeResultError, dispatch_codex_client,
    billing_probe_client, observe_provider_client, open_codex_client, orchestration_client, record_codex_result_client, status_client,
    HostOutcomeUnknown, _host_pair, operational_checks_client, record_operational_qa_client, learning_cycle_client,
    routing_evidence_client, owner_learning_client, orchestration_report_client, capacity_report_client, runtime_hint_path,
    run_control_client, bridged_run_owner, abandon_bridged_run,
)
from codex_agents.dispatch_api import DispatchError  # noqa: E402
from codex_agents.history_contracts import HistoryError  # noqa: E402
from codex_agents.project_host_client import (  # noqa: E402
    SERVICE_START_REASONS, ensure_project_host, other_version_host, project_host_serving, withdraw_project_host,
)
from codex_agents.admission_metadata import api_keys_present  # noqa: E402
from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS  # noqa: E402
from codex_agents.cli_reports import command_report, unavailable_cycle  # noqa: E402
from codex_agents.collector_contracts import (  # noqa: E402
    CollectorError,
    CollectorTransportError,
    CollectorRestartError,
    CollectorVersionError,
)
from codex_agents.collector_runtime import validate_collector_runtime  # noqa: E402
from codex_agents.learning_store import (  # noqa: E402
    CollectorMaintenance,
    LearningLedger,
    LearningState,
    LedgerCapacityError,
    LedgerError,
    StateError,
    authorize_collector_runtime,
    client_health_clear,
    copy_ledger_before_upgrade,
    default_state_root,
    ensure_collector,
    ledger_schema_behind,
    legacy_ledger_schema_behind,
    migrate_legacy_state,
    registered_project_roots,
    require_authorized_collector_runtime,
)
from codex_agents.hosts import (  # noqa: E402
    CLAUDE_HOST_CLIENT,
    CODEX_HOST_CLIENT,
    CODEX_THREAD_MARKERS,
    HOST_CLIENTS,
    OWNER_CLIENT,
    HostClientMismatch,
    HostClientUnknown,
    resolve_host_client,
)
from codex_agents.host_commands import (  # noqa: E402
    PRIMARY_HOST_COMMANDS,
    CodexHomeUnsupported,
    HostCommandError,
    OwnRootRefused,
    argv_prefix as codex_argv_prefix,
    bound_project_root,
    configured_codex_home,
    install_rule as install_codex_rule,
    installer_root,
    project_cli,
    remove_rule as remove_codex_rule,
    render_rule as render_codex_rule,
    rule_path as codex_rule_path,
    rule_status as codex_rule_status,
    runtime_binding,
    runtime_copy_problem,
    runtime_copy_root,
    shell_command,
)
from codex_agents.claude_host import (  # noqa: E402
    ClaudeHostDenied,
    check_backend,
    check_model_overrides,
    is_native_claude,
    native_spawn,
)
from codex_agents.providers import (  # noqa: E402
    AdmissionDenied,
    billing_attestation_record,
)
from codex_agents.routing import (  # noqa: E402
    PolicyError,
    _route_task_legacy_fallback,
    load_effective_policy,
    load_policy,
    route_task,
    verifier_setting,
)
from codex_agents.runtime_files import RELEASES_FILE  # noqa: E402
from codex_agents.session_guard import (  # noqa: E402
    SessionRefused,
    check_current_session,
    scan_recent_sessions,
    session_guard_status,
)
from codex_agents.validation import validate_package  # noqa: E402

# A protected runtime copy serves the project its owner-only binding names, never
# the place it sits; a binding that does not verify refuses every command.
_RUNTIME_BINDING_ERROR: str | None = None
_BINDING: dict[str, str] | None = None
try:
    _BOUND_PROJECT = bound_project_root(ORCHESTRATION_ROOT)
    _BINDING = runtime_binding(ORCHESTRATION_ROOT)
except HostCommandError as _binding_error:
    _BOUND_PROJECT, _RUNTIME_BINDING_ERROR = None, str(_binding_error)
PROTECTED_RUNTIME = _BOUND_PROJECT is not None or _RUNTIME_BINDING_ERROR is not None
if _BOUND_PROJECT is not None:
    BUNDLE_ROOT = _BOUND_PROJECT
# Signed release data is not runtime code: a protected copy reads its project's.
RELEASES_PATH = BUNDLE_ROOT / ".codex" / "orchestration" / RELEASES_FILE
# The copy runs only what the rule allows, plus the collector it starts.
PROTECTED_RUNTIME_COMMANDS = frozenset(PRIMARY_HOST_COMMANDS) | {"learning-serve"}


def _bounded_input(path: str) -> bytes:
    if path == "-":
        return sys.stdin.buffer.read(1_048_577)
    with Path(path).open("rb") as stream:
        return stream.read(1_048_577)


def _json_input(path: str) -> object:
    try:
        raw = _bounded_input(path)
    except OSError as exc:
        raise ContractError("input file is unavailable") from exc
    return parse_bounded_json_value(raw, max_bytes=1_048_576)


def _result_input(path: str) -> object:
    raw = _bounded_input(path)
    try:
        return parse_bounded_json_value(raw, max_bytes=1_048_576)
    except ContractError as exc:
        raise ResultContractError(
            "invalid_shape", "result must be one bounded JSON value"
        ) from exc


def _restore_lifecycle(
    ledger: LearningLedger, project_id: str, candidate_digest: str
) -> CandidateLifecycle:
    candidate, promotions = ledger.lifecycle_records(project_id, candidate_digest)
    return CandidateLifecycle.restore(
        candidate=candidate,
        champion_digest=candidate["base_digest"],
        project_id=project_id,
        authority=OwnerGrantAuthority.for_state(ledger.state),
        digest_key=ledger.state.digest_key(),
        promotions=promotions,
    )


def _bounded_json_input(path: str) -> dict:
    try:
        raw = sys.stdin.buffer.read(65_537) if path == "-" else Path(path).read_bytes()
    except OSError as exc:
        raise ContractError("input file is unavailable") from exc
    return parse_bounded_json(raw)


def _print(data: object) -> None:
    if type(data) is dict and data.get("ready") is False and type(data.get("reason_code")) is str:
        from codex_agents.refusal_steps import next_step
        # S1-H1b: what to do next for every refusal with a reason code, always the table's fixed text
        # (never a supplied value). A not-ready reply without one is a report, not a refusal.
        data = {**data, "next_step": next_step(data["reason_code"])}
    print(json.dumps(data, indent=2, sort_keys=True))


def _usage_report(args: argparse.Namespace) -> dict:
    """The usage report of one window, or a refusal with a fixed reason code; reads only."""
    import datetime

    def moment(text):
        try:
            value = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            raise UsageReportError("usage_window_invalid") from None
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)
        return int(value.timestamp() * 1000)

    try:
        return {"ready": True, **usage_report(CollectorPaths(args.state_root), args.project_root,
                                              moment(args.since), moment(args.until), args.limit)}
    except UsageReportError as refusal:
        return {"ready": False, "reason_code": refusal.reason_code}
    except StateError:
        return {"ready": False, "reason_code": "usage_state_unavailable"}


def _print_command_report(args: argparse.Namespace, report: dict) -> None:
    _print(command_report(args.command, report, full=getattr(args, "full_report", False)))


def _add_assignment_context(
    parser: argparse.ArgumentParser, *, allow_policy_override: bool = True
) -> None:
    parser.add_argument("--assignment", required=True, help="JSON file")
    parser.add_argument("--task", required=True, help="routed task JSON file")
    parser.add_argument("--parent-grant", required=True, help="parent authority JSON file")
    parser.add_argument(
        "--fallback-receipt",
        help="trusted fallback receipt issued from an abandoned primary host run",
    )
    if allow_policy_override:
        # None marks an absent --policy, which an installed CLI refuses; main fills
        # in the installed policy after that check.
        parser.add_argument("--policy", type=Path)


def _add_learning_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--project-root", required=True, type=Path)


_COLLECTOR_RESTART_ERROR = (
    "the collector was stopped for a moment and did not start again; "
    "run learning-status for a registered project"
)
_COMPLETED_LEDGER_RESTART_ERROR = (
    "ledger operation completed; collector restart failed; "
    "run learning-status for a registered project"
)


class _RestartingLedger:
    def __init__(
        self, ledger: LearningLedger, maintenance: CollectorMaintenance
    ) -> None:
        self._ledger = ledger
        self._maintenance = maintenance
        self._closed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._ledger, name)

    def close(self, *, operation_completed: bool) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._ledger.close()
        except Exception:
            try:
                self._maintenance.resume()
            except CollectorError:
                pass
            raise
        try:
            self._maintenance.resume()
        except CollectorError as exc:
            if operation_completed:
                raise CollectorRestartError(_COMPLETED_LEDGER_RESTART_ERROR) from exc


def _close_ledger(ledger: LearningLedger | _RestartingLedger) -> None:
    if isinstance(ledger, _RestartingLedger):
        ledger.close(operation_completed=sys.exc_info()[0] is None)
    else:
        ledger.close()


# Codex's sandbox blocks the collector's socket and state, so a sandboxed primary
# gets this alongside the unchanged error.
BILLING_ATTESTATION_HINT = (
    'Run ./setup.sh for this project and choose Configure, then "Re-run the billing attestation only".'
)

CODEX_SANDBOX_HINT = (
    "If this ran inside the Codex sandbox: run host-command --project-root "
    "<exact-root> and repeat this command with its argv_prefix; if codex_rule is not "
    "installed, ask the owner to run ./setup.sh (Codex commands step)."
)
# Set when this invocation failed to reach the learning state or collector.
_COLLECTOR_UNREACHED = False


def _note_collector_failure(exc: BaseException) -> None:
    """Note a failure to reach the learning state or collector.

    A collector that answered (a rejection code or a version mismatch) was reached.
    """

    global _COLLECTOR_UNREACHED
    if isinstance(exc, (OSError, CollectorTransportError)) or (
        isinstance(exc, CollectorError)
        and exc.code is None
        and not isinstance(exc, CollectorVersionError)
    ):
        _COLLECTOR_UNREACHED = True


@contextlib.contextmanager
def _collector_contact() -> Iterator[None]:
    try:
        yield
    except (OSError, CollectorError) as exc:
        _note_collector_failure(exc)
        raise


def _with_codex_hint(command: str, report: dict) -> dict:
    """Add the sandbox hint when a Codex primary-host command missed the collector."""

    if (
        _COLLECTOR_UNREACHED
        and command in PRIMARY_HOST_COMMANDS
        and any(os.environ.get(name) for name in CODEX_THREAD_MARKERS)
    ):
        return {**report, "hint": CODEX_SANDBOX_HINT}
    return report


def _start_collector_quietly(args: argparse.Namespace) -> None:
    """Best effort: a client that still finds no collector reports it unavailable.

    A failed start never stops the command itself from reporting what it can see.
    """
    try:
        ensure_collector(CollectorPaths(args.state_root), ORCHESTRATION_ROOT / "cli.py",
                         project_root=args.project_root)
    except StateError as error:
        # State that still sits in the old macOS folder needs the owner's setup
        # step; say so instead of reporting services that cannot exist yet.
        if str(error) == UNMOVED_STATE:
            raise
    except Exception:
        pass


def _compatible_collector(state):
    """The running collector when it serves exactly this runtime, else None (owner maintenance)."""
    try:
        client = CollectorClient(state.socket)
        if client.health().get("runtime_digest") != COLLECTOR_RUNTIME_DIGEST:
            return None
    except Exception:
        return None
    return client


_MIGRATION_MESSAGES = {
    "migration_not_drained": "a child run is still active or unsettled; nothing was changed, try again when none is running",
    "migration_project_service_running": "this project's service did not stop; nothing was changed, try again",
    "migration_source_changed": "the ledger changed while it was being copied; nothing was changed, run it again",
    "migration_reconciliation_required": "learning was recorded after the move; going back would drop it",
    "migration_too_large": "the project's learning history is larger than a migration can copy",
}


class ProjectServiceOtherVersion(Exception):
    """A live service of another version holds the project and could not be replaced."""


def _replace_other_version_host(args):
    """Withdraw a project service that another version started; whether none is left.

    Done with the collector stopped (it stops only when no run is active or
    unsettled, and no run can be claimed while it is down) and the ledger
    checked drained, so the service holds no run and cannot be given one,
    whatever version its clients are. The service is looked up only once this
    command holds maintenance: another command may have replaced it meanwhile,
    and the one serving now must not be the one withdrawn. The collector is
    started again either way, and a failure to start it is reported.
    """
    try:
        state = LearningState(args.state_root, create=False)
        maintenance = CollectorMaintenance.acquire(state, ORCHESTRATION_ROOT / "cli.py", args.project_root)
    except (CollectorError, StateError, OSError):
        return False
    replaced = False
    try:
        directory = other_version_host(args.state_root, args.project_root)
        if directory is None:
            replaced = True
        else:
            ledger = LearningLedger(state)
            try:
                # Runs an earlier version left never ran under a project service.
                drained = ledger.drained(children_only=True)
            finally:
                ledger.close()
            replaced = drained and withdraw_project_host(args.state_root, args.project_root, directory)
    except (LedgerError, StateError, OSError):
        replaced = False
    except BaseException:
        with contextlib.suppress(CollectorError):
            maintenance.resume()
        raise
    try:
        maintenance.resume()
    except CollectorError as error:
        raise CollectorRestartError(_COLLECTOR_RESTART_ERROR) from error
    return replaced


from codex_agents.learning_migration import START_REASONS, clear_start_failure, record_start_failure  # noqa: E402


def _start_reply(serving, migration_failure):
    """learning-start-project-service's reply; a start-time migration failure is part of it (O10 HS-8)."""
    report = {"ready": serving, "serving": serving}
    if migration_failure is not None:
        report["learning_migration_failure"] = migration_failure
    return report


def _service_slots(slots):
    """learning-status: the project service's run slots it can no longer free (O0c M1, M3), with one plain line."""
    stuck, limit = slots['stuck'], slots['limit']
    if stuck == 0:
        summary = f"Project service: none of its {limit} run slots is stuck."
    else:
        summary = (f"Project service: {stuck} of its {limit} run slots {'is' if stuck == 1 else 'are'} held by "
                   "runs it cannot finish (a run failed after its claim, or a child's end could not be confirmed). "
                   "It keeps them, since a child may still run, and it will be replaced by a fresh service once "
                   "no run is under way and it has been idle; the new service settles them.")
    return {'stuck_run_slots': stuck, 'limit': limit, 'replaced_when_idle': stuck > 0, 'summary': summary}


def _start_project_host(args):
    """Start this project's service unless one serves; first settle where its learning lives.

    A service that another version started (the project was updated while it was
    idle) is replaced, but only when no run is active anywhere; otherwise the
    command says so instead of reporting a service that cannot answer it.

    A project with no learning history in the shared ledger is simply marked as
    living in its own store. One with history is migrated here, before its
    service first holds the store, but only when no run is active anywhere;
    otherwise it stays pending and the next start tries again. Any failure
    leaves the project as it was: its service starts either way.
    """
    if other_version_host(args.state_root, args.project_root) is not None and not _replace_other_version_host(args):
        raise ProjectServiceOtherVersion()
    try:
        if not project_host_serving(args.state_root, args.project_root):
            _start_collector_quietly(args)
            client = _compatible_collector(CollectorPaths(args.state_root))
            if client is not None:
                snapshot = client.learning_migration(args.project_root)
                if not snapshot["moved"] and not snapshot["has_learning"]:
                    client.learning_moved_if_empty(args.project_root)
                elif (not snapshot["moved"] or snapshot["activation_pending"]) and snapshot["drained"]:
                    # Not yet moved, or retired by a migration that stopped before
                    # switching the copy on: run (or finish) it now. O10 HS-3: its outcome is kept for
                    # learning-status, never thrown away.
                    state = CollectorPaths(args.state_root)
                    began_ms = time.time_ns() // 1_000_000
                    try:
                        report = _migrate_learning(argparse.Namespace(
                            state_root=args.state_root, project_root=args.project_root, status=False, rollback=False))
                    except Exception:
                        report = {"ready": False, "reason_code": "migration_failed"}
                    if type(report) is dict and report.get("ready") is False:
                        # HS-8: in this start's reply as well as kept, so it shows even if keeping it fails.
                        reason = report.get("reason_code")
                        args.learning_migration_failure = (reason if type(reason) is str and reason in START_REASONS
                                                           else "migration_failed")
                        record_start_failure(state, args.project_root, args.learning_migration_failure)
                    else:
                        clear_start_failure(state, args.project_root, since_ms=began_ms)
    except Exception:
        pass
    # O10 HS-4: a service that ends at once hands back its own fixed reason.
    args.service_start_report = {}
    return ensure_project_host(args.state_root, ORCHESTRATION_ROOT, args.project_root,
                               report=args.service_start_report)


_MOVED_LEARNING_COMMANDS = frozenset({
    "learning-run-cycle", "learning-analyze", "learning-record-feedback", "learning-review-case",
    "learning-mine-case",
})


def _project_moved(state_root, project_root):
    """Whether the ledger is retired for this project's learning (even if a copy is still to be switched on)."""
    try:
        client = _compatible_collector(CollectorPaths(state_root))
        return client is not None and client.learning_migration(project_root)["moved"]
    except (CollectorError, StateError, OSError):
        return False


def _learning_moved(args):
    """Whether this project's learning history lives in its own store, asked of the collector."""
    _start_collector_quietly(args)
    try:
        client = _compatible_collector(CollectorPaths(args.state_root))
        if client is None:
            return False
        snapshot = client.learning_migration(args.project_root)
        return snapshot["moved"] and not snapshot["activation_pending"]
    except (CollectorError, StateError, OSError):
        return False


def _moved_learning_command(args):
    """An owner learning command for a project whose learning lives in its own store.

    The project's service does the work; the shared ledger is not opened and
    the collector is never paused.
    """
    if args.command == "learning-mine-case":
        raise ContractError(
            "cases are mined automatically for this project; run learning-run-cycle to mine now")
    _start_project_host(args)

    def cycle(seconds):
        report = learning_cycle_client(
            args.state_root, args.project_root, host_client=OWNER_CLIENT, full=True,
            **({"max_age_ms": args.max_age_ms} if getattr(args, "max_age_ms", None) else {}), seconds=seconds)
        return None if report.get("summary_state") == "unavailable" else report, report.get("reason_code")

    def unavailable(reason):
        return {"ready": False, "reason_code": reason, "error": "the project's learning service did not answer"}

    try:
        if args.command in {"learning-run-cycle", "learning-analyze"}:
            if args.max_age_ms < 1:
                raise ContractError("max age must be positive")
            report, reason = cycle(120.0)
            if report is None:
                return unavailable(reason)
            if args.command == "learning-analyze":
                # The cycle's own analysis, as of now.
                return {"ready": True, "analysis": report["analysis"]}
            # Nothing is deleted from the project store: retention is applied when it is read.
            return {"ready": True, **report, "expired_records": 0}
        if args.command == "learning-record-feedback":
            state = LearningState(args.state_root, create=False)
            record = build_feedback(
                digest_key=state.digest_key(), run_id=args.run_id, label=args.label,
                reason_code=args.reason_code, observed_at_ms=args.observed_at_ms,
                correction=args.correction_file.read_bytes() if args.correction_file is not None else None)
            if args.signature is None:
                project_id = CollectorClient(state.socket).project(args.project_root)
                return {"ready": True, "requires_signature": True, "record": record,
                        "signing_payload_b64": base64.b64encode(
                            OwnerGrantAuthority.owner_record_payload(project_id, record)).decode("ascii")}
            answer = owner_learning_client(args.state_root, args.project_root, "feedback",
                                           record=record, signature=args.signature.read_bytes())
        else:
            answer = owner_learning_client(
                args.state_root, args.project_root, "review_case", case_id=args.case_id,
                review_digest=args.review_digest, similarity_group_digest=args.similarity_group_digest,
                destination=args.destination, freeze=args.freeze)
            record = answer["record"]
    except HistoryError as error:
        if error.reason_code == "history_not_authorized" and args.command == "learning-record-feedback":
            return {"ready": False, "error": "owner record signature is invalid"}
        return unavailable(error.reason_code)
    if answer["refused"] is not None:
        return {"ready": False, "reason_code": answer["refused"],
                "error": answer["detail"] or _OWNER_REFUSALS[answer["refused"]]}
    report = {"ready": True, "stored": answer["stored"], "record": record}
    if args.command == "learning-review-case":
        report["learning_cycle"] = cycle(20.0)[0] or unavailable_cycle("learning_pending")
    return report


_OWNER_REFUSALS = {
    "feedback_run_unknown": "owner feedback run is not admitted",
    "case_unknown": "evaluation case is unavailable",
    "case_review_refused": "the review is not allowed for this case",
    "owner_record_conflict": "a different record already has this identity",
}


def _migration_state(snapshot):
    """complete, pending (still to copy, or copied but not yet switched on) or not_needed."""
    if snapshot["moved"]:
        return "pending" if snapshot["activation_pending"] else "complete"
    return "pending" if snapshot["has_learning"] else "not_needed"


def _migrate_learning(args):
    """Move, report or roll back one project's learning history: offline owner work.

    The collector is stopped only if no run is active anywhere, and is started
    again whatever happens. The project's own service must not be running.
    """
    from codex_agents.learning_migration import (
        MigrationError, migrate_project, migration_status, rollback_project,
    )
    from codex_agents.project_service import open_offline_history

    state = LearningState(args.state_root, create=False)
    if args.status:
        client = _compatible_collector(state)
        if client is not None:
            # Read from the running services; nothing is stopped for a question.
            with _collector_contact():
                snapshot = client.learning_migration(args.project_root)
            return {"ready": True, "can_start_now": snapshot["drained"], "state": _migration_state(snapshot),
                    "earlier_version_runs": snapshot["earlier_version_runs"]}

    def refused(reason):
        return {"ready": False, "reason_code": reason,
                "error": _MIGRATION_MESSAGES.get(reason, "the learning history could not be moved")}

    with _collector_contact():
        try:
            maintenance = CollectorMaintenance.acquire(state, ORCHESTRATION_ROOT / "cli.py", args.project_root)
        except CollectorError as error:
            if getattr(error, "code", None) == "collector_busy":
                return refused("migration_not_drained")
            raise
    ledger = history = None
    try:
        ledger = LearningLedger(state, allow_schema_upgrade=True)
        project_id = ledger.project_for_root(args.project_root)
        try:
            history = open_offline_history(state, project_id)
        except HistoryError as error:
            if error.reason_code != "history_writer_busy" or args.status:
                raise
            if not ledger.drained():
                # Say what holds it back when it is runs an earlier version left.
                return {**refused("migration_not_drained"), "earlier_version_runs": ledger.earlier_version_runs()}
            # No run is active anywhere, so this project's idle service is asked to
            # stop: it exits once its published hint is withdrawn, and the next
            # command starts another. Its journals are durable.
            runtime_hint_path(CollectorPaths(args.state_root), args.project_root).unlink(missing_ok=True)
            deadline = time.monotonic() + 5
            while history is None:
                time.sleep(0.2)
                try:
                    history = open_offline_history(state, project_id)
                except HistoryError as retry:
                    if retry.reason_code != "history_writer_busy" or time.monotonic() >= deadline:
                        raise
        if args.status:
            report = {**migration_status(ledger, history, project_id),
                      "earlier_version_runs": ledger.earlier_version_runs()}
        elif args.rollback:
            report = rollback_project(ledger, history, project_id)
        else:
            report = migrate_project(ledger, history, project_id, now_ms=time.time_ns() // 1_000_000)
        return {"ready": True, **report}
    except MigrationError as error:
        if error.reason_code == "migration_not_drained" and ledger is not None:
            try:
                return {**refused(error.reason_code), "earlier_version_runs": ledger.earlier_version_runs()}
            except Exception:  # The refusal stands without the detail.
                pass
        return refused(error.reason_code)
    except HistoryError as error:
        return refused("migration_project_service_running" if error.reason_code == "history_writer_busy"
                       else "migration_unavailable")
    finally:
        try:
            if history is not None:
                history.close()
            if ledger is not None:
                ledger.close()
        finally:
            maintenance.resume()


def _register_project(args, moved_aside, upgrade_required, progress):
    """The owner's registration once any old macOS records are in place; its report, or an exit code."""
    state = progress["state"] = LearningState(args.state_root)
    behind = ledger_schema_behind(state)
    if behind is not None and not args.upgrade_ledger:
        return upgrade_required(behind, moved_aside)
    running = (_compatible_collector(state)
               if moved_aside is None and args.move_legacy_state is None else None)
    if running is not None:
        # Another project's children keep the collector: register through it.
        project_id = running.register(args.project_root)
        health = running.health()
        return {
            "ready": True,
            "active": True,
            "project_id": project_id,
            "collector_schema_version": health["schema_version"],
            "collector_runtime_version": health["runtime_version"],
            "collector_runtime_digest": health["runtime_digest"],
            "state_moved": None,
            "ledger_upgraded_from": None,
            "ledger_copy": None,
        }
    maintenance = CollectorMaintenance.acquire(
        state,
        ORCHESTRATION_ROOT / "cli.py",
        args.project_root,
        owner_registration=True,
    )
    try:
        # Looked at again now that nothing else can write: only what is
        # still an earlier version's ledger is copied and upgraded.
        behind = progress["behind"] = ledger_schema_behind(state) if args.upgrade_ledger else None
        if behind is not None:
            progress["copy"] = copy_ledger_before_upgrade(state, behind)
        ledger = LearningLedger(state, allow_schema_upgrade=behind is not None,
                                backup_before_upgrade=behind is not None)
        try:
            project_id = ledger.register_project(args.project_root)
        finally:
            _close_ledger(ledger)
        authorize_collector_runtime(
            state, ORCHESTRATION_ROOT / "cli.py"
        )
        progress["registered"] = True
    except BaseException:
        try:
            maintenance.resume()
        except CollectorError:
            pass
        raise
    try:
        maintenance.resume()
    except CollectorError as exc:
        raise CollectorRestartError(
            "project registration completed; collector restart failed; "
            "run learning-status for the registered project"
        ) from exc
    health = ensure_collector(
        state,
        ORCHESTRATION_ROOT / "cli.py",
        project_root=args.project_root,
    )
    return {
        "ready": True,
        "active": True,
        "project_id": project_id,
        "collector_schema_version": health["schema_version"],
        "collector_runtime_version": health["runtime_version"],
        "collector_runtime_digest": health["runtime_digest"],
        "state_moved": str(moved_aside) if moved_aside is not None else None,
        "ledger_upgraded_from": behind,
        "ledger_copy": str(progress["copy"]) if progress["copy"] is not None else None,
    }


# A cause is shown only when it is fixed wording: no path, no identifier or digest, no other text.
_PLAIN_CAUSE = re.compile(r"[A-Za-z][A-Za-z0-9 ,;:()'_.-]{0,199}")
_IDENTIFYING = re.compile(r"[0-9a-f]{12,}|\b(?:prj|hrn|orc|brg|run)_|@")


def _plain_cause(failure) -> str | None:
    text = str(failure)
    if not _PLAIN_CAUSE.fullmatch(text) or _IDENTIFYING.search(text):
        return None
    return text


def _mebibytes(value) -> str:
    return f"{-(-int(value) // 1_048_576):,} MiB"


def _capacity_error(capacity) -> str:
    """One sentence about sizes only; the ledger was not touched."""
    if capacity.get("available_bytes") is not None:
        return ("the records were not upgraded and nothing was changed: their backups need about "
                f"{_mebibytes(capacity['needed_bytes'])} of free disk space and "
                f"{_mebibytes(capacity['available_bytes'])} is free; free some space and run setup again")
    which = ("a project" if capacity.get("project_number") is None
             else f"project {capacity['project_number']} of {capacity['projects']}")
    return (f"the records were not upgraded and nothing was changed: {which} needs a backup of about "
            f"{_mebibytes(capacity['needed_bytes'])}, over the {_mebibytes(capacity['limit_bytes'])} limit; "
            "keep using the earlier version and report this")


def _registration_failure(moved_aside, progress, failure):
    """What a failed registration tells the owner once records were moved or an upgrade began; else None.

    The schema change is one transaction, but the copy before it and the
    registration, authorization and collector start after it are not part of
    it: the ledger is read again to say which side the failure fell on, and a
    registration that completed is never described as unfinished.
    """
    behind, copy = progress["behind"], progress["copy"]
    if behind is None and moved_aside is None:
        return None
    kept = "; keep the copy of the records as they were" if copy is not None else ""
    if progress["registered"]:
        reason, error = "collector_not_started_after_registration", (
            ("the records were upgraded and " if behind is not None else "") +
            "this project is registered, but the collector did not start again; run setup again" + kept)
    elif behind is None:
        reason, error = "registration_incomplete_after_move", (
            "your records were moved to the protected folder, but registering this project did not finish; "
            "run setup again")
    else:
        try:
            still_behind = ledger_schema_behind(progress["state"]) is not None
        except Exception:
            still_behind = None
        if still_behind and isinstance(failure, LedgerCapacityError):
            reason, error = "ledger_upgrade_capacity", _capacity_error(failure.capacity)
        elif still_behind:
            reason, error = "ledger_upgrade_failed", (
                "the records could not be upgraded and are as the earlier version left them; "
                "install that version again to keep using them" + kept)
        elif still_behind is False:
            reason, error = "registration_incomplete_after_upgrade", (
                "the records were upgraded, but registering this project did not finish; run setup again" + kept)
        else:
            reason, error = "ledger_upgrade_unknown", (
                "the upgrade stopped and the records cannot be read to see how far it got; "
                "do not delete anything" + kept)
    return {
        "ready": False,
        "reason_code": reason,
        "error": error,
        # What failed, when that is the collector's or the ledger's own fixed text.
        "cause": (str(failure)[:200] if isinstance(failure, CollectorError)
                  else _plain_cause(failure) if isinstance(failure, (LedgerError, sqlite3.Error))
                  and not isinstance(failure, LedgerCapacityError) else None),
        **({"capacity": failure.capacity} if isinstance(failure, LedgerCapacityError) else {}),
        # Old macOS records moved by this same command stay moved.
        "state_moved": str(moved_aside) if moved_aside is not None else None,
        "ledger_schema_version": behind,
        "ledger_copy": str(copy) if copy is not None else None,
    }


def _host_qa(args):
    """Whether this project's host recorded the worker outcome QA names (else the legacy ledger did)."""
    try:
        _host_pair(args.state_root, args.project_root, args.host_outcome_digest, args.host_outcome_digest,
                   host_client=_host_client())
    except (HostOutcomeUnknown, HostClientUnknown):
        return False
    return True


def _host_driven(route):
    """A Codex route, or a bridged Claude child: the project host runs both."""
    return isinstance(route, dict) and (route.get('provider') == 'codex' or (
        route.get('provider') == 'claude' and route.get('dispatch') == 'bridge'))


def _refuse_bridged_run(args: argparse.Namespace, run_handle: str, refusal: str) -> None:
    """A run the project host drives is not changed through the legacy ledger."""
    owner = bridged_run_owner(args.state_root, args.project_root, run_handle, host_client=_host_client())
    if owner == 'other':
        raise HostClientMismatch("host_client_mismatch")
    if owner == 'owned':
        raise LedgerError(refusal)


def _resolved(path: Path) -> Path:
    return Path(os.path.realpath(Path(path).expanduser()))


def _require_protected_runtime(args: argparse.Namespace) -> None:
    """Refuse what a protected runtime copy must not run, before anything else.

    The copy is what the owner's Codex rule runs outside the sandbox. It
    serves only the primary-host commands (and the collector it starts), only
    while the project's installed runtime is still byte-for-byte this copy (a
    package update, or a change to the project's installed files, stops it
    until the owner runs setup again), and only for a Codex session whose own
    record shows it cannot write the folders the rule depends on
    (`session_guard`).
    """

    if not PROTECTED_RUNTIME:
        return
    if _RUNTIME_BINDING_ERROR is not None:
        raise HostCommandError(_RUNTIME_BINDING_ERROR)
    if args.command not in PROTECTED_RUNTIME_COMMANDS:
        raise HostCommandError("a protected runtime copy runs only the primary-host commands")
    if args.command == "learning-serve":
        return
    if runtime_copy_problem(ORCHESTRATION_ROOT.parent.parent, BUNDLE_ROOT) is not None:
        raise HostCommandError(
            "the project's installed runtime differs from its protected copy; ask the "
            "owner to run ./setup.sh (Codex commands step)"
        )
    # The calling Codex session may not be able to write anything the rule
    # depends on: its home folder, ~/.codex, this copy, the clone, tool folders,
    # the learning state, the private scratch, or a folder that contains the
    # project (or the project's .codex, .agents or .git as a folder of its own).
    try:
        check_current_session(
            ORCHESTRATION_ROOT.parent.parent,
            Path(_BINDING["clone_root"]) if _BINDING is not None else None,
            BUNDLE_ROOT,
        )
    except SessionRefused as exc:
        raise HostCommandError(str(exc)) from None


def _require_own_roots(args: argparse.Namespace) -> None:
    """Pin an installed CLI's primary-host commands to this project and its state.

    The owner's Codex command rule may run these commands outside the sandbox
    with arguments the model chose, so nothing may move a trust root: the state
    root must be the default one, the project root must be the project holding
    this CLI, and no policy file may be named. It keys on the install manifest
    next to this file, which a sandboxed Codex session cannot create or remove
    (`.codex/` is read-only there); the source tree keeps these options for
    development and tests. Host markers play no part. It runs before any git,
    process or collector work, and the checked, resolved values replace the
    given ones so nothing is resolved twice.
    """

    if args.command not in PRIMARY_HOST_COMMANDS or not os.path.lexists(INSTALL_MANIFEST):
        return
    if getattr(args, "policy", None) is not None:
        raise OwnRootRefused("--policy is refused: an installed CLI uses its installed policy")
    state_root = getattr(args, "state_root", None)
    if state_root is not None:
        if _resolved(state_root) != _resolved(default_state_root()):
            raise OwnRootRefused(
                "--state-root is refused: an installed CLI uses only the default learning state"
            )
        args.state_root = None
    for option, name in (("--project-root", "project_root"), ("--root", "root")):
        value = getattr(args, name, None)
        if value is None:
            continue
        if _resolved(value) != BUNDLE_ROOT:
            raise OwnRootRefused(
                f"{option} is refused: an installed CLI serves only its own project"
            )
        setattr(args, name, BUNDLE_ROOT)


def _learning_ledger(args: argparse.Namespace) -> tuple[_RestartingLedger, str]:
    with _collector_contact():
        state = LearningState(args.state_root, create=False)
        maintenance = CollectorMaintenance.acquire(
            state,
            ORCHESTRATION_ROOT / "cli.py",
            args.project_root,
        )
    try:
        ledger = LearningLedger(state)
    except Exception:
        maintenance.resume()
        raise
    managed = _RestartingLedger(ledger, maintenance)
    try:
        return managed, ledger.project_for_root(args.project_root)
    except Exception:
        managed.close(operation_completed=False)
        raise


def _validated_assignment_route(
    args: argparse.Namespace, *, trusted_host: bool = False
) -> tuple[dict, dict, dict]:
    task = _json_input(args.task)
    project_root = getattr(args, "project_root", None)
    if trusted_host:
        if project_root is None:
            raise ContractError("trusted route preflight requires a registered project")
        fast = _store_free_route(project_root, task, state_root=getattr(args, "state_root", None))
        if fast is not None:
            route = fast[0]
        else:
            ledger, project_id = _learning_ledger(args)
            try:
                route, _ = _trusted_host_route(ledger, project_id, project_root, task)
            finally:
                _close_ledger(ledger)
        assigned = validate_assignment(
            _json_input(args.assignment), task=task, route=route,
            parent_grant=_json_input(args.parent_grant),
        )
        return assigned, route, task
    policy = (
        load_effective_policy(
            project_root,
            args.policy,
            ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
        )
        if project_root is not None
        else load_policy(args.policy)
    )
    fallback_receipt = args.fallback_receipt
    if fallback_receipt is not None and project_root is None:
        raise ContractError("fallback receipt requires a registered project")
    if isinstance(task, dict) and task.get("verification_for_outcome") is not None:
        if project_root is None:
            raise LedgerError("verification_for_outcome requires --project-root")
        # The worker outcome itself is checked by the route and bound by the verifier's open,
        # through the project's service. Checking it here took the shared ledger for itself,
        # which pauses the collector and is refused while any child runs anywhere.
    if fallback_receipt is not None:
        ledger, project_id = _learning_ledger(args)
        try:
            route, _ = _trusted_host_route(
                ledger,
                project_id,
                project_root,
                task,
                fallback_receipt=fallback_receipt,
            )
        finally:
            _close_ledger(ledger)
    elif (
        project_root is not None
        and isinstance(task, dict)
        and task.get("routing_evidence_refs")
    ):
        ledger, project_id = _learning_ledger(args)
        try:
            trusted = ledger.trusted_routing_evidence_refs(project_id, task)
            route = route_task(
                task,
                policy,
                trusted_routing_evidence_refs=trusted,
                primary_provider=_host_primary_provider(),
            )
        finally:
            _close_ledger(ledger)
    else:
        route = route_task(task, policy, primary_provider=_host_primary_provider())
    assigned = validate_assignment(
        _json_input(args.assignment),
        task=task,
        route=route,
        parent_grant=_json_input(args.parent_grant),
    )
    return assigned, route, task


def _validated_assignment(args: argparse.Namespace) -> dict:
    return _validated_assignment_route(args)[0]


# Bridged children run under their own ledger client, which declares the
# client-observed usage the bridge reports.
BRIDGE_CLIENT = "bridge-v1"
# The primary provider follows the detected host client and never the task.
HOST_CLIENT_PRIMARY_PROVIDERS = {
    CODEX_HOST_CLIENT: "codex",
    CLAUDE_HOST_CLIENT: "claude",
}
# Set from --host-client; it must agree with the detected host.
_EXPLICIT_HOST_CLIENT: str | None = None


def _host_client() -> str:
    """The host driving this lifecycle step, from its environment markers.

    The markers only catch accidental misuse; see codex_agents.hosts for the
    trust model. Admission, the bridge flag and the sandbox are the gates.
    """

    return resolve_host_client(os.environ, _EXPLICIT_HOST_CLIENT)


def _host_primary_provider(client: str | None = None) -> str:
    provider = HOST_CLIENT_PRIMARY_PROVIDERS.get(client or _host_client())
    if provider is None:
        raise ContractError("host client has no trusted primary provider")
    return provider


def _require_host_owner(
    ledger: LearningLedger,
    project_id: str,
    *,
    orchestration_handle: str | None = None,
    run_handle: str | None = None,
) -> str:
    """Refuse a lifecycle mutation from a host other than the one that owns it.

    Ownership is the host client stored when the orchestration opened; the
    bridge's execution client is separate and never counts as an owner.
    """

    host = _host_client()
    owner = ledger.host_owner(
        project_id, orchestration_handle=orchestration_handle, run_handle=run_handle
    )
    if owner != host:
        raise HostClientMismatch("host_client_mismatch")
    return host


def _claude_host_open_checks(
    ledger: LearningLedger,
    project_id: str,
    host: str,
    route: dict,
    project_root: Path,
) -> tuple[dict, dict | None]:
    """Run the Claude-host open checks and return the run profile to record.

    Every Claude-host open checks for a metered backend, since its children
    inherit the session. A native Claude child also gets its spawn parameters
    from code, and an environment that would override its model is denied.
    """

    if host != CLAUDE_HOST_CLIENT:
        return {
            "host_client": host, "backend_check": "not_applicable", "model_pin": None,
            "subagent_type": None, "model_alias": None, "effort": None,
        }, None
    try:
        check_backend(os.environ, project_root)
        spawn = None
        if is_native_claude(route):
            check_model_overrides(os.environ, project_root)
            spawn = native_spawn(route)
    except ClaudeHostDenied as exc:
        ledger.record_host_denial(
            project_id, provider=route.get("provider", "codex"), reason_code=exc.reason_code
        )
        raise
    return {
        "host_client": host,
        "backend_check": "passed",
        "model_pin": "alias" if spawn is not None else None,
        "subagent_type": spawn["subagent_type"] if spawn else None,
        "model_alias": spawn["model_alias"] if spawn else None,
        "effort": spawn["effort"] if spawn else None,
    }, spawn


def _host_client_for(route: dict) -> str:
    return BRIDGE_CLIENT if route.get("dispatch") == "bridge" else _host_client()


def _store_free_route(project_root: Path, task: dict, client_version: str | None = None,
                      state_root: Path | None = None):
    """The route when it needs nothing from the ledger, else None.

    With an empty release registry there are no learning candidates to read, and
    routing evidence the project's own service recorded is confirmed by that
    service, so routing is policy: the CLI routes without opening the ledger or
    pausing the shared collector, which serves other projects' running children.
    Evidence the service does not hold is left to the ledger. A verifier's
    worker outcome is bound by its open, not here.
    """
    trusted = frozenset()
    if validate_task(task)["routing_evidence_refs"]:
        trusted = None if state_root is None else routing_evidence_client(
            state_root, project_root, validate_task(task), host_client=_host_client())
        if trusted is None:
            if state_root is not None and _project_moved(state_root, project_root):
                # The project's own store is the authority on what its owner has
                # rejected since; the ledger's frozen copy cannot vouch for a run.
                raise ContractError("routing evidence is not held by this project's service")
            return None
    try:
        if load_released_candidate_digests(RELEASES_PATH, verify_signature=None):
            return None
    except ContractError:
        return None  # A registry that needs the owner's signature check: the ledger path.
    policy = load_effective_policy(
        project_root,
        ORCHESTRATION_ROOT / "config" / "routing-policy.json",
        ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
    )
    route = route_task(task, policy, trusted_routing_evidence_refs=trusted,
                       primary_provider=_host_primary_provider())
    clients = load_client_capabilities(ORCHESTRATION_ROOT / "config" / "learning-clients.json")
    capability = clients[client_version or _host_client_for(route)]
    route["execution_configuration"] = installed_execution_configuration(
        BUNDLE_ROOT, route, client_capability=capability
    )
    route["learning_candidate_digests"] = []
    route["ready"] = True
    return route, capability


def _installed_route(args: argparse.Namespace, task: dict) -> dict:
    """The route the installed policy gives this task, from the ledger only when it must be."""
    fast = _store_free_route(args.project_root, task, state_root=args.state_root)
    if fast is not None:
        return fast[0]
    ledger, project_id = _learning_ledger(args)
    try:
        return _trusted_host_route(ledger, project_id, args.project_root, task)[0]
    finally:
        _close_ledger(ledger)


def _trusted_host_route(
    ledger: LearningLedger,
    project_id: str,
    project_root: Path,
    task: dict,
    *,
    fallback_receipt: str | None = None,
) -> tuple[dict, dict]:
    """Resolve the exact installed route used by the trusted host lifecycle."""

    policy = load_effective_policy(
        project_root,
        ORCHESTRATION_ROOT / "config" / "routing-policy.json",
        ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
    )
    trusted = ledger.trusted_routing_evidence_refs(project_id, task)
    # A verifier bound to a worker outcome is routed only when that outcome is
    # this project's, from the named provider and family, in an active boundary.
    # Open and dispatch read the result itself; recording needs only the binding.
    try:
        ledger.verification_worker_result(project_id, task, with_result=False)
    except LedgerError as error:
        # A worker the project host recorded is bound by its verifier's open instead.
        if str(error) != "verification_for_outcome is not a recorded host outcome":
            raise
    primary_route = route_task(
        task,
        policy,
        trusted_routing_evidence_refs=trusted,
        primary_provider=_host_primary_provider(),
    )
    clients = load_client_capabilities(
        ORCHESTRATION_ROOT / "config" / "learning-clients.json"
    )
    capability = clients[_host_client_for(primary_route)]
    primary_route["execution_configuration"] = installed_execution_configuration(
        BUNDLE_ROOT, primary_route, client_capability=capability
    )
    selection_slice = model_selection_slice(task)
    released = load_released_candidate_digests(
        RELEASES_PATH,
        verify_signature=lambda payload, signature: OwnerGrantAuthority.for_state(ledger.state).verify_release_signature(payload, signature),
    )
    applied = []
    for candidate in ledger.routing_candidates(
        project_id, selection_slice, released_candidate_digests=released
    ):
        primary_route = apply_candidate_to_route(
            primary_route,
            candidate,
            primary_route["execution_configuration"],
            policy,
        )
        applied.append(candidate["candidate_digest"])
    primary_route["learning_candidate_digests"] = applied
    primary_route["ready"] = True
    if fallback_receipt is None:
        return primary_route, capability
    fallback_authorization = ledger.verify_legacy_fallback_receipt(
        project_id,
        receipt_id=fallback_receipt,
        task=task,
        primary_route=primary_route,
        client_version=_host_client(),
    )
    route = _route_task_legacy_fallback(
        task,
        policy,
        trusted_routing_evidence_refs=trusted,
        primary_provider=_host_primary_provider(),
    )
    route["fallback"]["receipt_id"] = fallback_receipt
    route["fallback"]["authorization_digest"] = fallback_authorization[
        "authorization_digest"
    ]
    route["execution_configuration"] = installed_execution_configuration(
        BUNDLE_ROOT, route, client_capability=capability
    )
    route["learning_candidate_digests"] = []
    route["ready"] = True
    return route, capability


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a native package")
    validate_parser.add_argument("--root", type=Path, default=BUNDLE_ROOT)
    validate_parser.add_argument(
        "--package-only",
        action="store_true",
        help="exit on package readiness while still reporting project readiness",
    )

    inventory_parser = subparsers.add_parser("inventory", help="discover definitions inertly")
    inventory_parser.add_argument("--root", type=Path, default=BUNDLE_ROOT)
    inventory_parser.add_argument("--policy", type=Path)

    route_parser = subparsers.add_parser("route", help="route one task contract")
    route_parser.add_argument("--task", required=True, help="JSON file or - for stdin")
    route_parser.add_argument("--root", type=Path, default=BUNDLE_ROOT)
    route_parser.add_argument("--policy", type=Path)

    learning_route = subparsers.add_parser(
        "learning-route", help="route one task with approved learning lifecycle state"
    )
    learning_route.add_argument("--task", required=True)
    learning_route.add_argument("--client-version")
    learning_route.add_argument(
        "--fallback-from-run-handle",
        help="abandoned primary host run proving model unavailability",
    )
    _add_learning_project(learning_route)

    learning_canary_route = subparsers.add_parser(
        "learning-route-canary",
        help="resolve one reviewed canary case through its trusted ledger binding",
    )
    learning_canary_route.add_argument("--case-id", required=True)
    learning_canary_route.add_argument("--task", required=True)
    _add_learning_project(learning_canary_route)

    result_parser = subparsers.add_parser("validate-result", help="validate one result")
    result_parser.add_argument("--result", required=True, help="JSON file or - for stdin")
    result_parser.add_argument("--state-root", type=Path)
    result_parser.add_argument("--project-root", type=Path)
    _add_assignment_context(result_parser)

    result_preflight_parser = subparsers.add_parser(
        "result-preflight",
        help="print the exact non-mutating result template and admissibility rules",
    )
    result_preflight_parser.add_argument("--state-root", type=Path)
    result_preflight_parser.add_argument("--project-root", type=Path)
    _add_assignment_context(result_preflight_parser)

    dispatch_preview = subparsers.add_parser(
        "dispatch-preflight", help="preview committed bridge files and configured effective capabilities; does not admit or launch",
    )
    dispatch_preview.add_argument("--project-root", required=True, type=Path)
    dispatch_preview.add_argument("--state-root", type=Path)
    dispatch_preview.add_argument("--offset", type=int, default=0)
    dispatch_preview.add_argument("--limit", type=int, default=50)
    dispatch_preview.add_argument("--expected-export-commit")
    _add_assignment_context(dispatch_preview)

    assignment_parser = subparsers.add_parser(
        "validate-assignment", help="validate one bounded subagent assignment"
    )
    _add_assignment_context(assignment_parser)
    assignment_parser.add_argument("--project-root", type=Path)
    assignment_parser.add_argument("--state-root", type=Path)

    evaluate_parser = subparsers.add_parser("evaluate", help="aggregate evaluation JSONL")
    evaluate_parser.add_argument("--records", required=True, type=Path)
    evaluate_parser.add_argument(
        "--policy",
        type=Path,
        default=ORCHESTRATION_ROOT / "config" / "routing-policy.json",
    )

    learning_validate = subparsers.add_parser(
        "learning-validate", help="validate one closed learning record"
    )
    learning_validate.add_argument("--record", required=True, help="JSON file or -")

    learning_register = subparsers.add_parser(
        "learning-register", help="register one trusted project root"
    )
    learning_register.add_argument("--state-root", type=Path)
    learning_register.add_argument("--project-root", required=True, type=Path)
    learning_register.add_argument(
        "--move-legacy-state",
        help="approve moving the pre-0.8.10 macOS state: the SHA-256 of its "
        "owner-allowed-signers file as reviewed, or 'none'",
    )
    learning_register.add_argument(
        "--upgrade-ledger", action="store_true",
        help="approve upgrading records an earlier version wrote; a checked copy is kept first",
    )

    learning_serve = subparsers.add_parser(
        "learning-serve", help="run the single-writer local collector"
    )
    learning_serve.add_argument("--state-root", type=Path)

    learning_start_service = subparsers.add_parser(
        "learning-start-project-service",
        help="start this project's service now, as the next child would (S1-U8: setup runs it after an update)",
    )
    learning_start_service.add_argument("--state-root", type=Path)
    learning_start_service.add_argument("--project-root", required=True, type=Path)

    learning_status = subparsers.add_parser(
        "learning-status", help="report persistent collector and redacted client health"
    )
    learning_status.add_argument("--state-root", type=Path)
    learning_status.add_argument("--project-root", required=True, type=Path)

    learning_backup = subparsers.add_parser(
        "learning-backup-project",
        help="quiesce the writer and create a recoverable project-partition backup",
    )
    _add_learning_project(learning_backup)
    learning_backup.add_argument("--file-id", required=True)
    learning_backup.add_argument("--expires-in-seconds", type=int, default=604800)

    learning_verify_backup = subparsers.add_parser(
        "learning-verify-backup",
        help="verify and restore a registered project backup in isolated memory",
    )
    _add_learning_project(learning_verify_backup)
    learning_verify_backup.add_argument("--file-id", required=True)

    learning_delete = subparsers.add_parser(
        "learning-delete-project", help="delete one registered project partition"
    )
    learning_delete.add_argument("--state-root", type=Path)
    learning_delete.add_argument("--project-root", required=True, type=Path)
    learning_delete.add_argument("--confirm-project-id", required=True)

    learning_app = subparsers.add_parser(
        "learning-normalize-app", help="normalize one App Server notification"
    )
    learning_app.add_argument("--state-root", type=Path)
    learning_app.add_argument("--message", required=True, help="JSON file or -")
    learning_app.add_argument("--client-version", required=True)

    learning_ingest_app = subparsers.add_parser(
        "learning-ingest-app", help="normalize and enqueue one App Server notification"
    )
    learning_ingest_app.add_argument("--state-root", type=Path)
    learning_ingest_app.add_argument("--project-root", required=True, type=Path)
    learning_ingest_app.add_argument("--message", required=True, help="JSON file or -")
    learning_ingest_app.add_argument("--client-version", required=True)

    learning_compare = subparsers.add_parser(
        "learning-compare", help="compare one inert candidate with matched trials"
    )
    learning_compare.add_argument("--candidate", required=True)
    learning_compare.add_argument("--trials", required=True)
    learning_compare.add_argument("--cases", required=True)
    learning_compare.add_argument("--client-version", required=True)
    learning_compare.add_argument("--independent-review-digest", required=True)
    learning_compare.add_argument("--grader-calibration")
    learning_compare.add_argument("--subjective-grading-required", action="store_true")

    learning_host_result = subparsers.add_parser(
        "learning-record-host-result",
        help="derive and atomically persist a host result and QA outcome",
    )
    _add_learning_project(learning_host_result)
    _add_assignment_context(learning_host_result, allow_policy_override=False)
    learning_host_result.add_argument("--result", required=True)
    learning_host_result.add_argument("--observation", required=True)
    learning_host_result.add_argument('--route', help='original Codex route used by open and dispatch')
    learning_host_result.add_argument("--evaluation-assignment-digest")
    learning_host_result.add_argument("--trial-id")
    learning_host_result.add_argument(
        "--full-report", action="store_true",
        help="include the complete learning-cycle analysis and record identifiers",
    )

    learning_open_host = subparsers.add_parser(
        "learning-open-host-run",
        help="open one exact assigned host run and return a bounded opaque handle",
    )
    _add_learning_project(learning_open_host)
    _add_assignment_context(learning_open_host, allow_policy_override=False)
    learning_open_host.add_argument("--route", required=True)
    learning_open_host.add_argument("--orchestration-handle", required=True)

    dispatch_parser = subparsers.add_parser(
        "dispatch",
        help="run one admitted, opened host run as a read-only bridged child",
    )
    _add_learning_project(dispatch_parser)
    _add_assignment_context(dispatch_parser, allow_policy_override=False)
    dispatch_parser.add_argument("--route", required=True)
    dispatch_parser.add_argument("--run-handle", required=True)
    dispatch_parser.add_argument("--expected-export-commit", help="refuse before launch if HEAD differs from this preflight commit")

    provider_observe = subparsers.add_parser(
        "provider-observe",
        help="record a non-spending provider identity, headroom and billing observation",
    )
    _add_learning_project(provider_observe)
    provider_observe.add_argument(
        "--provider", required=True, choices=("codex", "claude")
    )
    provider_observe.add_argument(
        "--billing-probe",
        action="store_true",
        help="for Claude, also run one minimal locked turn to record billing evidence",
    )
    provider_observe.add_argument('--request-id', help='reuse the exact identity to recover an observation or billing-probe reply')
    provider_observe.add_argument('--run-handle', help='original opened run for a before/after observation')
    provider_observe.add_argument('--phase', choices=('before', 'after'))

    learning_billing_attestation = subparsers.add_parser(
        "learning-record-billing-attestation",
        help="record an owner-signed billing attestation for one provider account",
    )
    _add_learning_project(learning_billing_attestation)
    learning_billing_attestation.add_argument(
        "--provider", required=True, choices=("codex", "claude")
    )
    learning_billing_attestation.add_argument("--grant", required=True)
    learning_billing_attestation.add_argument("--signature", type=Path)

    learning_open_orchestration = subparsers.add_parser(
        "learning-open-orchestration",
        help="open one goal-scoped orchestration statistics boundary",
    )
    _add_learning_project(learning_open_orchestration)

    learning_checkpoint_orchestration = subparsers.add_parser(
        "learning-checkpoint-orchestration",
        help="record exact native-goal usage while an orchestration remains active",
    )
    _add_learning_project(learning_checkpoint_orchestration)
    learning_checkpoint_orchestration.add_argument(
        "--orchestration-handle", required=True
    )
    learning_checkpoint_orchestration.add_argument(
        "--goal-tokens-used", required=True, type=int
    )
    learning_checkpoint_orchestration.add_argument(
        "--goal-time-used-seconds", required=True, type=int
    )

    learning_pause_orchestration = subparsers.add_parser(
        "learning-pause-orchestration",
        help="persist an explicit pause without closing the orchestration",
    )
    _add_learning_project(learning_pause_orchestration)
    learning_pause_orchestration.add_argument("--orchestration-handle", required=True)

    learning_resume_orchestration = subparsers.add_parser(
        "learning-resume-orchestration",
        help="resume an explicitly paused orchestration without resetting history",
    )
    _add_learning_project(learning_resume_orchestration)
    learning_resume_orchestration.add_argument("--orchestration-handle", required=True)

    learning_close_orchestration = subparsers.add_parser(
        "learning-close-orchestration",
        help="close one orchestration boundary and return its statistics",
    )
    _add_learning_project(learning_close_orchestration)
    learning_close_orchestration.add_argument(
        "--orchestration-handle", required=True
    )
    learning_close_orchestration.add_argument(
        "--status",
        required=True,
        choices=("completed", "failed", "interrupted", "cancelled"),
    )
    learning_close_orchestration.add_argument("--goal-tokens-used", type=int)
    learning_close_orchestration.add_argument("--goal-time-used-seconds", type=int)

    for lifecycle in (learning_open_orchestration, learning_checkpoint_orchestration,
                      learning_pause_orchestration, learning_resume_orchestration, learning_close_orchestration):
        lifecycle.add_argument('--request-id', help='reuse the exact identity to reconcile a lost reply')
    for lifecycle in (learning_checkpoint_orchestration, learning_pause_orchestration,
                      learning_resume_orchestration, learning_close_orchestration):
        lifecycle.add_argument('--expected-sequence', type=int,
            help='original sequence for an explicit retry; otherwise read current state once')
    learning_orchestration_status = subparsers.add_parser('learning-orchestration-status',
        help='read current owned lifecycle metadata through the project API')
    _add_learning_project(learning_orchestration_status)
    learning_orchestration_status.add_argument('--orchestration-handle', required=True)
    learning_orchestration_status.add_argument('--request-id')

    review = subparsers.add_parser('learning-review',
        help='run one review: worker, verify or finish (S1-H1d)')
    review.add_argument('stage', choices=('worker', 'verify', 'finish'))
    _add_learning_project(review)
    review.add_argument('--spec', required=True, type=Path)
    review.add_argument('--work-dir', required=True, type=Path)
    review.add_argument('--status', default='completed', help='the status finish closes the orchestration with')
    run_status = subparsers.add_parser('learning-run-status',
        help="read a running child's live status through its project host")
    run_cancel = subparsers.add_parser('learning-cancel-run',
        help='request cancellation of a running child; acceptance is not cleanup')
    for run_control in (run_status, run_cancel):
        _add_learning_project(run_control)
        run_control.add_argument('--orchestration-handle', required=True)
        run_control.add_argument('--run-handle', required=True)
        run_control.add_argument('--attempt', type=int, default=1)

    learning_orchestration_report = subparsers.add_parser(
        "learning-orchestration-report",
        help="report content-free statistics for one orchestration",
    )
    _add_learning_project(learning_orchestration_report)
    learning_orchestration_report.add_argument("--orchestration-id", required=True)

    learning_usage_report = subparsers.add_parser(
        "learning-usage-report",
        help="read-only usage of the children a project ran in a window, per provider and model and per run",
    )
    _add_learning_project(learning_usage_report)
    learning_usage_report.add_argument("--since", required=True, help="window start, ISO 8601 (inclusive)")
    learning_usage_report.add_argument("--until", required=True, help="window end, ISO 8601 (exclusive)")
    learning_usage_report.add_argument("--limit", type=int, default=USAGE_REPORT_ROWS,
                                       help="runs listed at most (default %(default)s)")

    learning_bind_agent = subparsers.add_parser(
        "learning-bind-host-agent",
        help="bind a host run to the native subagent identifier",
    )
    _add_learning_project(learning_bind_agent)
    learning_bind_agent.add_argument("--run-handle", required=True)
    learning_bind_agent.add_argument("--agent-id", required=True)

    learning_observe_host = subparsers.add_parser(
        "learning-observe-host-run",
        help="persist progress or an elapsed observation deadline for one host run",
    )
    _add_learning_project(learning_observe_host)
    learning_observe_host.add_argument("--run-handle", required=True)
    learning_observe_host.add_argument(
        "--event", required=True, choices=("progress", "deadline")
    )

    learning_abandon_host = subparsers.add_parser(
        "learning-classify-host-run-abandoned",
        help="classify one pending host binding after its orchestration is paused",
    )
    _add_learning_project(learning_abandon_host)
    learning_abandon_host.add_argument("--run-handle", required=True)
    learning_abandon_host.add_argument(
        "--reason",
        required=True,
        choices=("host_confirmed_gone", "owner_cancelled", "recovery_unavailable"),
    )

    learning_prepare_upgrade = subparsers.add_parser(
        "learning-prepare-upgrade-reconciliation",
        help="prepare one exact superseded host run for detached owner signing",
    )
    _add_learning_project(learning_prepare_upgrade)
    learning_prepare_upgrade.add_argument("--orchestration-id", required=True)
    learning_prepare_upgrade.add_argument("--run-handle", required=True)

    learning_commit_upgrade = subparsers.add_parser(
        "learning-commit-upgrade-reconciliation",
        help="consume one signed superseded-run request and interrupt its boundary",
    )
    _add_learning_project(learning_commit_upgrade)
    learning_commit_upgrade.add_argument("--request", required=True)
    learning_commit_upgrade.add_argument("--signature", required=True)

    learning_verified_outcome = subparsers.add_parser(
        "learning-record-verified-outcome",
        help="derive QA from a durable assigned host run, checks, and verifier result",
    )
    _add_learning_project(learning_verified_outcome)
    learning_verified_outcome.add_argument(
        "--evaluation-assignment-digest", required=True
    )
    learning_verified_outcome.add_argument("--host-outcome-digest", required=True)
    learning_verified_outcome.add_argument(
        "--verifier-host-outcome-digest", required=True
    )
    learning_verified_outcome.add_argument("--evaluator-assignment", required=True)
    learning_verified_outcome.add_argument("--evaluator-result", required=True)
    learning_verified_outcome.add_argument("--check-report", required=True)
    learning_verified_outcome.add_argument("--verifier-assignment", required=True)
    learning_verified_outcome.add_argument("--verifier-result", required=True)
    learning_verified_outcome.add_argument("--signature", type=Path)

    learning_operational_qa = subparsers.add_parser(
        "learning-record-operational-qa",
        help="record automatic QA for two durable host-bound runs",
    )
    _add_learning_project(learning_operational_qa)
    learning_operational_qa.add_argument("--host-outcome-digest", required=True)
    learning_operational_qa.add_argument(
        "--verifier-host-outcome-digest", required=True
    )
    learning_operational_qa.add_argument("--evaluator-assignment", required=True)
    learning_operational_qa.add_argument("--evaluator-result", required=True)
    learning_operational_qa.add_argument("--check-receipt", required=True)
    learning_operational_qa.add_argument("--verifier-assignment", required=True)
    learning_operational_qa.add_argument("--verifier-result", required=True)
    learning_operational_qa.add_argument(
        "--full-report", action="store_true",
        help="include the complete learning-cycle analysis and record identifiers",
    )

    learning_run_checks = subparsers.add_parser(
        "learning-run-operational-checks",
        help="run project-approved checks and issue one bound QA receipt",
    )
    _add_learning_project(learning_run_checks)
    learning_run_checks.add_argument("--host-outcome-digest", required=True)
    learning_run_checks.add_argument(
        "--verifier-host-outcome-digest", required=True
    )

    learning_expire = subparsers.add_parser(
        "learning-expire", help="apply retention and lineage deletion for one project"
    )
    _add_learning_project(learning_expire)
    learning_expire.add_argument("--now-ms", type=int)

    learning_migrate = subparsers.add_parser(
        "learning-migrate-project",
        help="move one project's learning history from the shared ledger to its own project store",
    )
    _add_learning_project(learning_migrate)
    migrate_mode = learning_migrate.add_mutually_exclusive_group()
    migrate_mode.add_argument("--status", action="store_true", help="report only; nothing is changed")
    migrate_mode.add_argument(
        "--rollback", action="store_true",
        help="make the shared ledger the learning store again (refused once new learning was recorded)")

    learning_cycle = subparsers.add_parser(
        "learning-run-cycle",
        help="run deterministic aggregation, case mining, and inert proposal generation",
    )
    _add_learning_project(learning_cycle)
    learning_cycle.add_argument("--max-age-ms", type=int, default=90 * 24 * 60 * 60 * 1000)

    learning_assignment = subparsers.add_parser(
        "learning-create-evaluation-assignment",
        help="persist the complete matched evaluation population before trials run",
    )
    _add_learning_project(learning_assignment)
    learning_assignment.add_argument("--candidate-digest", required=True)
    learning_assignment.add_argument("--case-ids", required=True)
    learning_assignment.add_argument("--client-version", required=True)
    learning_assignment.add_argument("--observed-at-ms", required=True, type=int)

    learning_trial = subparsers.add_parser(
        "learning-record-trial", help="derive and persist one pre-assigned trial"
    )
    _add_learning_project(learning_trial)
    learning_trial.add_argument("--evaluation-assignment-digest", required=True)
    learning_trial.add_argument("--trial-id", required=True)
    learning_trial.add_argument("--outcome-digest", required=True)

    learning_review_evidence = subparsers.add_parser(
        "learning-record-evidence-review",
        help="record a post-population review of owner-attested verified subjects",
    )
    _add_learning_project(learning_review_evidence)
    learning_review_evidence.add_argument("--subject-digests", required=True)

    learning_record_comparison = subparsers.add_parser(
        "learning-record-comparison",
        help="derive and persist a comparison from the complete assigned population",
    )
    _add_learning_project(learning_record_comparison)
    learning_record_comparison.add_argument(
        "--evaluation-assignment-digest", required=True
    )
    learning_record_comparison.add_argument(
        "--independent-review-digest", required=True
    )
    learning_record_comparison.add_argument("--grader-calibration-digest")
    learning_record_comparison.add_argument(
        "--subjective-grading-required", action="store_true"
    )

    learning_mine = subparsers.add_parser(
        "learning-mine-case", help="mine one development case from an admitted outcome"
    )
    _add_learning_project(learning_mine)
    learning_mine.add_argument("--source-outcome-id", required=True)
    learning_mine.add_argument("--task", required=True)
    learning_mine.add_argument("--criteria-digest", required=True)
    learning_mine.add_argument("--lineage-digest", required=True)

    learning_review = subparsers.add_parser(
        "learning-review-case", help="review and freeze one development case"
    )
    _add_learning_project(learning_review)
    learning_review.add_argument("--case-id", required=True)
    learning_review.add_argument("--review-digest", required=True)
    learning_review.add_argument("--similarity-group-digest", required=True)
    learning_review.add_argument(
        "--destination", required=True, choices=("promotion_holdout", "canary")
    )
    learning_review.add_argument("--freeze", action="store_true")
    learning_review.add_argument(
        "--full-report", action="store_true",
        help="include the complete learning-cycle analysis and record identifiers",
    )

    learning_analyze = subparsers.add_parser(
        "learning-analyze", help="analyze admitted project outcomes by configuration"
    )
    _add_learning_project(learning_analyze)
    learning_analyze.add_argument("--as-of-ms", required=True, type=int)
    learning_analyze.add_argument("--max-age-ms", required=True, type=int)

    learning_candidate = subparsers.add_parser(
        "learning-generate-candidate", help="generate and store one inert typed candidate"
    )
    _add_learning_project(learning_candidate)
    learning_candidate.add_argument("--spec", required=True)

    learning_calibration = subparsers.add_parser(
        "learning-calibrate-grader", help="record owner-labelled grader calibration"
    )
    _add_learning_project(learning_calibration)
    learning_calibration.add_argument("--labels", required=True)
    learning_calibration.add_argument("--configuration", required=True)
    learning_calibration.add_argument("--observed-at-ms", required=True, type=int)
    learning_calibration.add_argument("--signature", type=Path)

    learning_feedback = subparsers.add_parser(
        "learning-record-feedback", help="record controlled owner feedback for one admitted run"
    )
    _add_learning_project(learning_feedback)
    learning_feedback.add_argument("--run-id", required=True)
    learning_feedback.add_argument(
        "--label", required=True, choices=("accepted", "rejected", "corrected")
    )
    learning_feedback.add_argument(
        "--reason-code",
        required=True,
        choices=("quality", "incorrect", "incomplete", "unsafe", "usage", "scope", "other"),
    )
    learning_feedback.add_argument("--correction-file", type=Path)
    learning_feedback.add_argument("--observed-at-ms", required=True, type=int)
    learning_feedback.add_argument("--signature", type=Path)

    learning_evaluate_candidate = subparsers.add_parser(
        "learning-evaluate-candidate", help="record a durable candidate evaluation decision"
    )
    _add_learning_project(learning_evaluate_candidate)
    learning_evaluate_candidate.add_argument("--candidate-digest", required=True)
    learning_evaluate_candidate.add_argument("--comparison-digest", required=True)
    learning_evaluate_candidate.add_argument("--observed-at-ms", required=True, type=int)

    learning_start_canary = subparsers.add_parser(
        "learning-start-canary", help="consume one owner grant and start its bounded canary"
    )
    _add_learning_project(learning_start_canary)
    learning_start_canary.add_argument("--candidate-digest", required=True)
    learning_start_canary.add_argument("--comparison-digest", required=True)
    learning_start_canary.add_argument("--case-digests", required=True)
    learning_start_canary.add_argument("--grant", required=True)

    learning_complete_canary = subparsers.add_parser(
        "learning-complete-canary", help="derive and record a canary decision from durable outcomes"
    )
    _add_learning_project(learning_complete_canary)
    learning_complete_canary.add_argument("--candidate-digest", required=True)
    learning_complete_canary.add_argument("--comparison-digest", required=True)
    learning_complete_canary.add_argument("--independent-review-digest", required=True)
    learning_complete_canary.add_argument("--case-results", required=True)
    learning_complete_canary.add_argument("--observed-at-ms", required=True, type=int)

    learning_promote = subparsers.add_parser(
        "learning-promote", help="consume a separate owner grant after a passing canary"
    )
    _add_learning_project(learning_promote)
    learning_promote.add_argument("--candidate-digest", required=True)
    learning_promote.add_argument("--canary-report-digest", required=True)
    learning_promote.add_argument("--grant", required=True)

    learning_drift = subparsers.add_parser(
        "learning-record-drift", help="record rejecting matched evidence for an active candidate"
    )
    _add_learning_project(learning_drift)
    learning_drift.add_argument("--candidate-digest", required=True)
    learning_drift.add_argument("--comparison-digest", required=True)
    learning_drift.add_argument("--observed-at-ms", required=True, type=int)

    learning_rollback = subparsers.add_parser(
        "learning-rollback", help="record exact failback to the candidate champion"
    )
    _add_learning_project(learning_rollback)
    learning_rollback.add_argument("--candidate-digest", required=True)
    learning_rollback.add_argument("--evidence-digest", required=True)
    learning_rollback.add_argument("--observed-at-ms", required=True, type=int)

    learning_owner_key = subparsers.add_parser(
        "learning-configure-owner-key", help="install one owner public SSH key"
    )
    learning_owner_key.add_argument("--state-root", type=Path)
    learning_owner_key.add_argument("--public-key", required=True, type=Path)

    learning_replace_owner_key = subparsers.add_parser(
        "learning-replace-owner-key", help="replace a lost owner public SSH key"
    )
    _add_learning_project(learning_replace_owner_key)
    learning_replace_owner_key.add_argument("--public-key", required=True, type=Path)

    learning_grant_request = subparsers.add_parser(
        "learning-create-grant-request", help="create exact bytes for detached owner signing"
    )
    _add_learning_project(learning_grant_request)
    learning_grant_request.add_argument(
        "--kind", required=True, choices=("canary", "promotion")
    )
    learning_grant_request.add_argument("--candidate-digest", required=True)
    learning_grant_request.add_argument("--champion-digest", required=True)
    learning_grant_request.add_argument("--review-digest", required=True)
    learning_grant_request.add_argument("--bounds-digest", required=True)
    learning_grant_request.add_argument("--issued-at-ms", required=True, type=int)
    learning_grant_request.add_argument("--expires-at-ms", required=True, type=int)

    learning_attach_signature = subparsers.add_parser(
        "learning-attach-grant-signature", help="verify and attach a detached owner signature"
    )
    learning_attach_signature.add_argument("--state-root", type=Path)
    learning_attach_signature.add_argument("--request", required=True)
    learning_attach_signature.add_argument("--signature", required=True, type=Path)

    # Read-only and collector-free, so a sandboxed Codex primary can run it first.
    host_command = subparsers.add_parser(
        "host-command",
        help="print the exact command prefix a Codex primary uses and its rule status",
    )
    host_command.add_argument("--project-root", required=True, type=Path)

    install_codex_rule = subparsers.add_parser(
        "install-codex-rule",
        help="owner setup: write the Codex command rule for one project",
    )
    install_codex_rule.add_argument("--project-root", required=True, type=Path)
    install_codex_rule.add_argument(
        "--replace", action="store_true", help="replace a differing rule for this project"
    )
    install_codex_rule.add_argument(
        "--print", dest="print_only", action="store_true",
        help="print the rule that would be written, without writing it",
    )

    remove_codex_rule_parser = subparsers.add_parser(
        "remove-codex-rule",
        help="owner removal: delete the Codex command rule and runtime copy for one project",
    )
    remove_codex_rule_parser.add_argument("--project-root", required=True, type=Path)

    # Read-only: which recent Codex sessions could write this clone, the home folder,
    # the learning state, or a folder that contains a set-up project.
    session_scan = subparsers.add_parser(
        "codex-session-scan",
        help="setup: list recent Codex sessions that could write this clone, the home "
        "folder, the learning state or a folder containing a set-up project",
    )
    session_scan.add_argument("--clone-root", required=True, type=Path)

    for subparser in subparsers.choices.values():
        subparser.add_argument(
            "--host-client",
            choices=HOST_CLIENTS,
            help="expected host client; refused unless it matches the detected host",
        )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    global _EXPLICIT_HOST_CLIENT, _COLLECTOR_UNREACHED
    _EXPLICIT_HOST_CLIENT = args.host_client
    _COLLECTOR_UNREACHED = False
    try:
        _require_protected_runtime(args)
        _require_own_roots(args)
        if args.command == "dispatch-preflight" and args.policy is not None:
            raise OwnRootRefused("--policy is refused: dispatch preflight uses its installed policy")
        if getattr(args, "policy", False) is None:
            args.policy = ORCHESTRATION_ROOT / "config" / (
                "discovery-policy.json" if args.command == "inventory" else "routing-policy.json"
            )
        if args.command == "validate":
            report = validate_package(args.root)
        elif args.command == "inventory":
            report = discover(args.root, load_discovery_policy(args.policy))
        elif args.command == "route":
            report = route_task(
                _json_input(args.task),
                load_effective_policy(
                    args.root,
                    args.policy,
                    ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
                ),
                primary_provider=_host_primary_provider(),
            )
        elif args.command == "learning-route":
            task = _json_input(args.task)
            policy = load_effective_policy(
                args.project_root,
                ORCHESTRATION_ROOT / "config" / "routing-policy.json",
                ORCHESTRATION_ROOT / "config" / "discovery-policy.json",
            )
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            explicit_client = args.client_version
            if explicit_client in HOST_CLIENTS:
                # An explicit host must be the detected one; it is then treated
                # exactly like the implicit host, bridge client included.
                if explicit_client != _host_client():
                    raise HostClientUnknown("host_client_unknown")
                explicit_client = None
            client_version = explicit_client or _host_client()
            if client_version not in clients:
                raise ContractError(
                    "client version is not in the validated capability matrix"
                )
            selection_slice = model_selection_slice(task)
            fast = (_store_free_route(args.project_root, task, explicit_client, state_root=args.state_root)
                    if args.fallback_from_run_handle is None else None)
            if fast is not None:
                report = fast[0]
                _print_command_report(args, report)
                return 0
            ledger, project_id = _learning_ledger(args)
            try:
                trusted = ledger.trusted_routing_evidence_refs(project_id, task)
                try:
                    ledger.verification_worker_result(project_id, task)
                except LedgerError as error:
                    # A worker the project host recorded lives in project history,
                    # not this ledger; its verifier's open binds and checks it there.
                    if str(error) != "verification_for_outcome is not a recorded host outcome":
                        raise
                base_route = route_task(
                    task,
                    policy,
                    trusted_routing_evidence_refs=trusted,
                    primary_provider=_host_primary_provider(),
                )
                if explicit_client is None:
                    # A bridged route runs under the bridge client, as the host
                    # lifecycle will when it opens the run.
                    client_version = _host_client_for(base_route)
                configuration = installed_execution_configuration(
                    BUNDLE_ROOT, base_route, client_capability=clients[client_version]
                )
                released = load_released_candidate_digests(
                    RELEASES_PATH,
                    verify_signature=lambda payload, signature: OwnerGrantAuthority.for_state(ledger.state).verify_release_signature(payload, signature),
                )
                applied = []
                report = dict(base_route)
                report["execution_configuration"] = dict(configuration)
                for candidate in ledger.routing_candidates(
                    project_id,
                    selection_slice,
                    released_candidate_digests=released,
                ):
                    report = apply_candidate_to_route(
                        report, candidate, report["execution_configuration"], policy
                    )
                    applied.append(candidate["candidate_digest"])
                report["learning_candidate_digests"] = applied
                report["ready"] = True
                if args.fallback_from_run_handle is not None:
                    _require_host_owner(
                        ledger, project_id, run_handle=args.fallback_from_run_handle
                    )
                    if base_route.get("dispatch") == "bridge":
                        # The fallback answers a native host's model rejection; a
                        # bridged child (every Codex child) ends fenced instead.
                        raise ContractError(
                            "the legacy fallback applies only to native children"
                        )
                    receipt = ledger.issue_legacy_fallback_receipt(
                        project_id,
                        run_handle=args.fallback_from_run_handle,
                        task=task,
                        primary_route=report,
                        client_version=client_version,
                    )
                    report = _route_task_legacy_fallback(
                        task,
                        policy,
                        trusted_routing_evidence_refs=trusted,
                        primary_provider=_host_primary_provider(),
                    )
                    report["fallback"]["receipt_id"] = receipt["receipt_id"]
                    report["fallback"]["authorization_digest"] = receipt[
                        "authorization_digest"
                    ]
                    report["execution_configuration"] = (
                        installed_execution_configuration(
                            BUNDLE_ROOT,
                            report,
                            client_capability=clients[client_version],
                        )
                    )
                    report["learning_candidate_digests"] = []
                    report["ready"] = True
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-route-canary":
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            ledger, project_id = _learning_ledger(args)
            try:
                case, candidate = ledger.canary_routing_candidate(
                    project_id, args.case_id
                )
                task = _json_input(args.task)
                task_slice = routing_task_slice(task)
                if (
                    task["id"] != case["record_id"]
                    or task_slice != case["task_slice"]
                    or runtime_fixture_digest(ledger.state.digest_key(), task)
                    != case["fixture_digest"]
                ):
                    raise ContractError("canary task does not match its frozen fixture")
                champion = candidate["champion_configuration"]
                matching_clients = [
                    (version, capability)
                    for version, capability in clients.items()
                    if content_digest(capability) == champion["client_digest"]
                ]
                if len(matching_clients) != 1:
                    raise ContractError(
                        "canary case client does not resolve to exactly one accepted capability"
                    )
                client_version, client_capability = matching_clients[0]
                policy = load_policy(
                    ORCHESTRATION_ROOT / "config" / "routing-policy.json"
                )
                trusted = ledger.trusted_routing_evidence_refs(project_id, task)
                report = route_task(
                    task,
                    policy,
                    trusted_routing_evidence_refs=trusted,
                    primary_provider=_host_primary_provider(),
                )
                current_configuration = installed_execution_configuration(
                    BUNDLE_ROOT,
                    report,
                    client_capability=client_capability,
                )
                if current_configuration != champion:
                    raise ContractError(
                        "canary champion differs from the installed task route"
                    )
                report["execution_configuration"] = current_configuration
                report = apply_candidate_to_route(
                    report, candidate, current_configuration, policy
                )
                report.update(
                    ready=True,
                    task_slice=case["task_slice"],
                    client_version=client_version,
                    canary_case_id=case["record_id"],
                    canary_case_digest=content_digest(case),
                    learning_candidate_digests=[candidate["candidate_digest"]],
                )
            finally:
                _close_ledger(ledger)
        elif args.command == "validate-assignment":
            report = _validated_assignment(args)
        elif args.command == "result-preflight":
            report = {"ready": True, **result_preflight(_validated_assignment(args))}
        elif args.command == "dispatch-preflight":
            if args.fallback_receipt is not None:
                raise ContractError("bridged preflight never uses a fallback receipt")
            assignment, route, task = _validated_assignment_route(args, trusted_host=True)
            report = dispatch_preflight(
                args.project_root, task, route, assignment,
                offset=args.offset, limit=args.limit, expected_commit=args.expected_export_commit,
            )
        elif args.command == "validate-result":
            assignment = _validated_assignment(args)
            try:
                report = validate_result(_result_input(args.result), assignment=assignment)
            except ResultContractError as exc:
                repair_receipt = None
                learning_ready: bool | None = None
                if args.project_root is not None:
                    learning_ready = False
                    try:
                        state = CollectorPaths(args.state_root)
                        ensure_collector(
                            state,
                            ORCHESTRATION_ROOT / "cli.py",
                            project_root=args.project_root,
                        )
                        assignment_digest = keyed_digest(
                            state.digest_key(),
                            canonical_json(validate_assignment_shape(assignment)),
                        )
                        repair_receipt = CollectorClient(
                            state.socket
                        ).issue_result_repair(
                            args.project_root,
                            assignment_digest,
                            exc.reason_code,
                        )
                        learning_ready = True
                    except (CollectorError, StateError, OSError) as failure:
                        _note_collector_failure(failure)
                _print(
                    _with_codex_hint(args.command, {
                        "ready": False,
                        "error": "result validation failed",
                        "reason_code": exc.reason_code,
                        "repair_receipt": repair_receipt,
                        "learning_ready": learning_ready,
                    })
                )
                return 2
        elif args.command == "evaluate":
            report = evaluate(load_jsonl(args.records), load_policy(args.policy))
        elif args.command == "learning-validate":
            report = validate_learning_record(_bounded_json_input(args.record))
        elif args.command == "learning-register":
            validate_collector_runtime(ORCHESTRATION_ROOT / "cli.py")

            def upgrade_required(schema, moved):
                # Asked before anything is stopped, moved or changed: the earlier
                # version's collector keeps running and its projects keep working.
                _print_command_report(args, {
                    "ready": False,
                    "reason_code": "ledger_upgrade_required",
                    "ledger_schema_version": schema,
                    "error": "these records were written by an earlier version; setup upgrades them "
                             "when you approve it (learning-register --upgrade-ledger)",
                    "state_moved": str(moved) if moved is not None else None,
                })
                return 2

            # Old macOS records the owner has approved as theirs would be moved and
            # then need the upgrade too: both decisions are collected before either
            # is acted on, so declining the second leaves the first undone.
            if (args.state_root is None and args.move_legacy_state is not None and not args.upgrade_ledger):
                legacy_behind = legacy_ledger_schema_behind()
                if legacy_behind is not None:
                    return upgrade_required(legacy_behind, None)
            # The owner's registration copies a pre-0.8.10 macOS state folder to the
            # default location first (once) and renames the old one aside; nothing
            # else does. `state_moved` names the old copy, which the owner may delete.
            moved_aside = (
                migrate_legacy_state(
                    ORCHESTRATION_ROOT / "cli.py",
                    args.project_root,
                    approved_signers=args.move_legacy_state,
                )
                if args.state_root is None
                else None
            )
            # From here on old records may have been moved, and an approved upgrade may
            # be under way: whatever then fails, the owner is told how far it got and
            # where the records and their copy are.
            progress: dict[str, Any] = {"state": None, "behind": None, "copy": None, "registered": False}
            try:
                report = _register_project(args, moved_aside, upgrade_required, progress)
            except Exception as failure:
                report = _registration_failure(moved_aside, progress, failure)
                if report is None:
                    raise
                _print_command_report(args, report)
                return 2
            if type(report) is int:
                return report
        elif args.command == "learning-serve":
            # The collector and the account services run from their own entry,
            # so this client imports none of the account persistence owners.
            state = LearningState(args.state_root, create=False)
            require_authorized_collector_runtime(state)
            sys.stdout.flush()
            os.execv(sys.executable, [sys.executable, *PYTHON_ISOLATION_FLAGS,
                                      str(ORCHESTRATION_ROOT / "collector_host.py"), "--state-root", str(state.root)])
        elif args.command == "learning-start-project-service":
            # As every command that needs it: an idle earlier version's service is replaced and a
            # project with history is moved first when nothing runs (S1-U8).
            serving = _start_project_host(args) is True
            report = _start_reply(serving, getattr(args, "learning_migration_failure", None))
            if not serving:
                reason = getattr(args, "service_start_report", {}).get("reason_code")
                report.update(reason_code=reason if reason in SERVICE_START_REASONS else "project_service_unavailable",
                              error="this project's service did not start; run learning-status for the collector")
                _print(report)
                return 2
        elif args.command == "learning-status":
            _start_collector_quietly(args)
            report = status_client(args.state_root, args.project_root)
            try:
                label = _host_client()
            except HostClientUnknown:
                label = OWNER_CLIENT
            # How near each limit of this project's store is (0.3.4 R9); None while its service is not running.
            report['project_capacity'] = capacity_report_client(args.state_root, args.project_root, host_client=label)
            # O0c M1, M3: the run slots this project's service can no longer free, said plainly.
            slots = (report['project_capacity'] or {}).pop('run_slots', None)
            if slots is not None:
                report['project_service'] = _service_slots(slots)
            if report.get('reason_code') == 'status_collector_transport_unavailable':
                _COLLECTOR_UNREACHED = True
                report = _with_codex_hint(args.command, report)
        elif args.command == "learning-backup-project":
            if args.expires_in_seconds < 60:
                raise ContractError("backup expiry must be at least 60 seconds")
            ledger, project_id = _learning_ledger(args)
            try:
                path = ledger.create_sanitized_backup(
                    project_id,
                    file_id=args.file_id,
                    expires_at_ms=(
                        time.time_ns() // 1_000_000
                        + args.expires_in_seconds * 1000
                    ),
                )
                verified = ledger.verify_sanitized_backup(
                    project_id, file_id=args.file_id
                )
                report = {
                    "ready": True,
                    "project_id": project_id,
                    "path": str(path),
                    **verified,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-verify-backup":
            ledger, project_id = _learning_ledger(args)
            try:
                report = {
                    "ready": True,
                    "project_id": project_id,
                    **ledger.verify_sanitized_backup(
                        project_id, file_id=args.file_id
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-delete-project":
            ledger, project_id = _learning_ledger(args)
            try:
                if args.confirm_project_id != project_id:
                    raise LedgerError("project deletion confirmation does not match")
                report = {
                    "ready": True,
                    "project_id": project_id,
                    "deletion_id": ledger.delete_project(project_id),
                }
                client_health_clear(ledger.state, args.project_root)
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-normalize-app":
            state = CollectorPaths(args.state_root)
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            if args.client_version not in clients:
                raise ContractError("client version is not in the validated capability matrix")
            normalized = normalize_app_server(
                _bounded_json_input(args.message),
                digest_key=state.digest_key(),
                client_version=args.client_version,
                coverage=clients[args.client_version]["app_server_coverage"],
            )
            report = {"ready": normalized is not None, "record": normalized}
        elif args.command == "learning-ingest-app":
            state = CollectorPaths(args.state_root)
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            if args.client_version not in clients:
                raise ContractError("client version is not in the validated capability matrix")
            message = _bounded_json_input(args.message)
            normalized = normalize_app_server(
                message,
                digest_key=state.digest_key(),
                client_version=args.client_version,
                coverage=clients[args.client_version]["app_server_coverage"],
            )
            params = message.get("params")
            thread_id = params.get("threadId") if isinstance(params, dict) else None
            if normalized is None or not isinstance(thread_id, str) or not thread_id:
                raise ContractError("App Server notification is not ingestible")
            client = CollectorClient(state.socket)
            binding = client.bind(
                args.project_root, thread_id, "app_server", args.client_version
            )
            report = {"ready": True, **client.ingest(binding, normalized)}
        elif args.command == "learning-compare":
            trials = _json_input(args.trials)
            cases = _json_input(args.cases)
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            if args.client_version not in clients:
                raise ContractError("client version is not in the validated capability matrix")
            if not isinstance(trials, list) or not isinstance(cases, list):
                raise ContractError("trials and cases must be arrays")
            report = compare_candidate(
                candidate=_json_input(args.candidate),
                trials=trials,
                cases=cases,
                client_version=args.client_version,
                client_capability=clients[args.client_version],
                independent_review_digest=args.independent_review_digest,
                grader_calibration=(
                    _json_input(args.grader_calibration)
                    if args.grader_calibration else None
                ),
                subjective_grading_required=args.subjective_grading_required,
            )
            report["ready"] = report["decision"] == "promote"
        elif args.command == "learning-open-host-run":
            task = _json_input(args.task)
            supplied_route = _json_input(args.route)
            assignment = _json_input(args.assignment)
            parent_grant = _json_input(args.parent_grant)
            # This only rejects malformed/unsupported requests. The supplied
            # route remains untrusted until the ledger resolves and compares it.
            validate_assignment(assignment, task=task, route=supplied_route,
                                parent_grant=parent_grant)
            if _host_driven(supplied_route):
                # Model, effort, agent and sandbox come only from the installed route.
                if args.fallback_receipt is None and supplied_route != _installed_route(args, task):
                    raise ContractError("supplied route does not match the installed route")
                _start_project_host(args)
                host = _host_client()  # Consistency only; native identity is independently checked.
                if host == CLAUDE_HOST_CLIENT:
                    check_backend(os.environ, args.project_root.resolve())
                if args.fallback_receipt is not None:
                    raise ContractError('bridged open never uses a fallback receipt')
                if api_keys_present(os.environ):
                    # The same admission refusal the account owner would give, before any request.
                    raise AdmissionDenied('provider_api_key_present')
                report = open_codex_client(args.state_root, args.project_root, args.orchestration_handle,
                    task, supplied_route, assignment, parent_grant, host_client=host)
                if report['state'] == 'denied':
                    raise AdmissionDenied(report['reason_code'])
                _print_command_report(args, report)
                return 0
            ledger, project_id = _learning_ledger(args)
            try:
                host = _require_host_owner(
                    ledger, project_id, orchestration_handle=args.orchestration_handle
                )
                route, _ = _trusted_host_route(
                    ledger,
                    project_id,
                    args.project_root,
                    task,
                    fallback_receipt=args.fallback_receipt,
                )
                if supplied_route != route:
                    raise ContractError("supplied route does not match the installed route")
                assignment = validate_assignment(
                    assignment,
                    task=task,
                    route=route,
                    parent_grant=parent_grant,
                )
                verification_input = ledger.verification_worker_result(
                    project_id, task, orchestration_handle=args.orchestration_handle
                )
                host_profile, spawn = _claude_host_open_checks(
                    ledger, project_id, host, route, args.project_root.resolve()
                )
                # The ledger admits the run itself as it opens it; later steps read
                # the stored decision. A verifier is admitted when its run opens.
                report = {
                    "ready": True,
                    **ledger.open_host_run(
                        project_id,
                        assignment=assignment,
                        task=task,
                        route=route,
                        execution_configuration=route["execution_configuration"],
                        client_version=_host_client_for(route),
                        orchestration_handle=args.orchestration_handle,
                        fallback_receipt=args.fallback_receipt,
                        host_profile=host_profile,
                    ),
                    **({"native_spawn": spawn} if spawn is not None else {}),
                    # A native verifier cannot be handed a file by the bridge, so
                    # the primary puts this ledger-read result in its prompt. A
                    # bridged verifier gets it from `dispatch` instead.
                    **(
                        {"verification_input": {
                            "host_outcome_digest": verification_input["host_outcome_digest"],
                            "worker_result": verification_input["worker_result"],
                        }}
                        if verification_input is not None
                        and route.get("dispatch") != "bridge"
                        else {}
                    ),
                    # Native children of either provider run with the parent
                    # session's permissions, so only a bridged Claude child's
                    # reads are contained.
                    "read_containment": (
                        "enforced"
                        if route.get("dispatch") == "bridge"
                        and route.get("provider") == "claude"
                        else "advisory"
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "dispatch":
            if args.fallback_receipt is not None:
                raise ContractError("bridged dispatch never uses a fallback receipt")
            task = _json_input(args.task)
            route = _json_input(args.route)
            if not isinstance(route, dict):
                raise ContractError("route must be an object")
            assignment = _json_input(args.assignment)
            parent_grant = _json_input(args.parent_grant)
            validate_assignment(assignment, task=task, route=route, parent_grant=parent_grant)
            if _host_driven(route):
                _host_client()  # Existing consistency check; markers confer no authority.
                report = {"ready": True, **dispatch_codex_client(
                    args.state_root, args.project_root, args.run_handle, task, route, assignment, parent_grant,
                    expected_export_commit=args.expected_export_commit)}
                _print_command_report(args, report)
                return 0
            # O10 D9: the bridge runs only routes the project host drives (bridge.dispatch refuses
            # every other one), so no child is ever started here.
            raise BridgeRefused("route is not run by the project host")
        elif args.command == "provider-observe":
            if api_keys_present(os.environ) and not args.billing_probe:
                # The account service observes with a clean environment; a session that
                # carries an API key is still told so here, as every earlier version did.
                # (A billing probe is denied by its own admission, with its reason code.)
                raise ContractError("provider API key environment variables must not be set")
            _start_collector_quietly(args)
            if not args.billing_probe:
                report = observe_provider_client(args.state_root, args.project_root, args.provider,
                    request_id=args.request_id, run_handle=args.run_handle, phase=args.phase)
            else:
                if args.run_handle is not None or args.phase is not None:
                    raise RuntimeBillingProbeError('probe_invalid_request')
                if args.provider != 'claude':
                    raise ContractError("the billing probe is for Claude only")
                report = billing_probe_client(args.state_root, args.project_root, request_id=args.request_id)
            _print_command_report(args, report)
            return 0
        elif args.command == "learning-record-billing-attestation" and _compatible_collector(
                LearningState(args.state_root, create=False)) is not None:
            # The owner's signed attestation through the running collector; nothing pauses it.
            running = _compatible_collector(LearningState(args.state_root, create=False))
            record = billing_attestation_record(_json_input(args.grant))
            if record["provider"] != args.provider:
                raise ContractError("billing grant provider does not match")
            if args.signature is None:
                report = {"ready": True, "requires_signature": True, "record": record,
                          "signing_payload_b64": base64.b64encode(
                              running.owner_record_payload(args.project_root, record)).decode("ascii")}
            else:
                report = {"ready": True, **running.commit_owner_record(
                    args.project_root, record, args.signature.read_bytes()), "record": record}
        elif args.command == "learning-record-billing-attestation":
            ledger, project_id = _learning_ledger(args)
            try:
                record = billing_attestation_record(_json_input(args.grant))
                if record["provider"] != args.provider:
                    raise ContractError("billing grant provider does not match")
                authority = OwnerGrantAuthority.for_state(ledger.state)
                payload = authority.owner_record_payload(project_id, record)
                if args.signature is None:
                    report = {
                        "ready": True,
                        "requires_signature": True,
                        "record": record,
                        "signing_payload_b64": base64.b64encode(payload).decode("ascii"),
                    }
                else:
                    report = {
                        "ready": True,
                        **ledger.commit_owner_record(
                            project_id,
                            record=record,
                            signature=args.signature.read_bytes(),
                        ),
                        "record": record,
                    }
            finally:
                _close_ledger(ledger)
        elif args.command in {
            'learning-open-orchestration', 'learning-checkpoint-orchestration', 'learning-pause-orchestration',
            'learning-resume-orchestration', 'learning-close-orchestration', 'learning-orchestration-status',
        }:
            operation = ('status' if args.command == 'learning-orchestration-status'
                         else args.command.removeprefix('learning-').removesuffix('-orchestration'))
            # Best effort: a client that still finds no service reports it unavailable.
            _start_project_host(args)
            report = orchestration_client(args.state_root, args.project_root, 'orchestration.'+operation,
                host_client=_host_client(), orchestration_handle=getattr(args, 'orchestration_handle', None),
                request_id=args.request_id, expected_sequence=getattr(args, 'expected_sequence', None),
                goal_tokens_used=getattr(args, 'goal_tokens_used', None),
                goal_time_used_seconds=getattr(args, 'goal_time_used_seconds', None), status=getattr(args, 'status', None))
            _print_command_report(args, report)
            return 0
        elif args.command == 'learning-review':
            from codex_agents.review_runner import ReviewError, run as run_review
            try:
                spec = json.loads(args.spec.read_text())
            except (OSError, ValueError):
                spec = None
            try:
                summary = run_review(args.stage, spec, args.work_dir, args.project_root, status=args.status,
                                     state_root=args.state_root)
            except ReviewError as stopped:
                _print({'ready': False, 'error': 'review stopped', 'reason_code': stopped.reason_code,
                        **({'step': stopped.detail} if stopped.detail else {})})
                return 2
            _print({'ready': True, 'stage': args.stage, **summary})
            return 0
        elif args.command in {'learning-run-status', 'learning-cancel-run'}:
            report = run_control_client(args.state_root, args.project_root,
                'project.status' if args.command == 'learning-run-status' else 'project.cancel',
                args.orchestration_handle, args.run_handle, args.attempt, host_client=_host_client())
            _print_command_report(args, report)
            return 0 if report['ready'] else 2
        elif args.command == "learning-orchestration-report":
            # The project's own service reports the orchestrations it ran; the
            # ledger still answers for those an earlier version kept there.
            _start_project_host(args)
            try:
                label = _host_client()
            except HostClientUnknown:
                label = OWNER_CLIENT
            held = orchestration_report_client(
                args.state_root, args.project_root, args.orchestration_id, host_client=label)
            if held is not None:
                report = {"ready": True, **held}
            else:
                ledger, project_id = _learning_ledger(args)
                try:
                    report = {
                        "ready": True,
                        **ledger.orchestration_report(
                            project_id, args.orchestration_id
                        ),
                    }
                finally:
                    _close_ledger(ledger)
        elif args.command == "learning-usage-report":
            # Read-only: no service is started and nothing is written (RT-8).
            report = _usage_report(args)
            _print(report)
            return 0 if report["ready"] else 2
        elif args.command == "learning-bind-host-agent":
            _refuse_bridged_run(args, args.run_handle, "a bridged run binds only its dispatched child")
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, run_handle=args.run_handle
                )
                report = {
                    "ready": True,
                    **ledger.bind_host_agent(
                        project_id,
                        run_handle=args.run_handle,
                        agent_id=args.agent_id,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-observe-host-run":
            _refuse_bridged_run(args, args.run_handle, "a bridged run is observed by its project host")
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, run_handle=args.run_handle
                )
                report = {
                    "ready": True,
                    **ledger.observe_host_run(
                        project_id,
                        run_handle=args.run_handle,
                        event=args.event,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-classify-host-run-abandoned":
            owner = bridged_run_owner(args.state_root, args.project_root, args.run_handle, host_client=_host_client())
            if owner == 'other':
                raise HostClientMismatch("host_client_mismatch")
            if owner == 'owned':
                report = abandon_bridged_run(args.state_root, args.project_root, args.run_handle, args.reason,
                                             host_client=_host_client())
                _print_command_report(args, report)
                return 0 if report['ready'] else 2
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, run_handle=args.run_handle
                )
                report = {
                    "ready": True,
                    **ledger.classify_host_run_abandoned(
                        project_id,
                        run_handle=args.run_handle,
                        reason=args.reason,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-prepare-upgrade-reconciliation":
            ledger, project_id = _learning_ledger(args)
            try:
                _require_host_owner(
                    ledger, project_id, run_handle=args.run_handle
                )
                request = ledger.prepare_upgrade_reconciliation(
                    project_id,
                    orchestration_id=args.orchestration_id,
                    run_handle=args.run_handle,
                )
                report = {
                    "ready": True,
                    "request": request,
                    "signing_payload_b64": base64.b64encode(
                        canonical_json(request)
                    ).decode("ascii"),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-commit-upgrade-reconciliation":
            request = _json_input(args.request)
            signature = _bounded_input(args.signature)
            ledger, project_id = _learning_ledger(args)
            try:
                report = {
                    "ready": True,
                    **ledger.commit_upgrade_reconciliation(
                        project_id, request=request, signature=signature
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-host-result":
            task = _json_input(args.task)
            result = _json_input(args.result)
            observation = _bounded_json_input(args.observation)
            clients = load_client_capabilities(
                ORCHESTRATION_ROOT / "config" / "learning-clients.json"
            )
            trusted_host = "run_handle" in observation
            # The host is detected unconditionally. Legacy telemetry recording
            # (no run handle) is a Codex path: only a Codex host, for a Codex run.
            host = _host_client()
            client_version = host if trusted_host else observation.get("client_version")
            if not trusted_host and (
                host != CODEX_HOST_CLIENT
                or not isinstance(client_version, str)
                or not client_version.startswith("codex-")
            ):
                raise HostClientMismatch("host_client_mismatch")
            if client_version not in clients:
                raise ContractError("client version is not in the validated capability matrix")
            if (args.evaluation_assignment_digest is None) != (args.trial_id is None):
                raise ContractError(
                    "evaluation assignment digest and trial id must appear together"
                )
            assignment = _json_input(args.assignment)
            supplied_route = None if args.route is None else _json_input(args.route)
            derived_route = False
            if (trusted_host and supplied_route is None and args.fallback_receipt is None
                    and args.evaluation_assignment_digest is None):
                owner = bridged_run_owner(args.state_root, args.project_root, observation.get('run_handle'),
                                          host_client=host)
                if owner == 'other':
                    raise HostClientMismatch("host_client_mismatch")
                if owner == 'owned':
                    # A run the project host drives: the command derives its route itself.
                    supplied_route, derived_route = _installed_route(args, task), True
            if trusted_host and (provider_for_model(assignment.get('model')) == 'codex' or _host_driven(supplied_route)):
                if supplied_route is None:
                    _refuse_bridged_run(args, observation['run_handle'], "a bridged run must be dispatched before its result")
                    raise RuntimeResultError('history_invalid_input')
                if args.evaluation_assignment_digest is not None or args.fallback_receipt is not None:
                    raise RuntimeResultError('history_invalid_input')
                route = supplied_route
                parent_grant = _json_input(args.parent_grant)
                assignment = validate_assignment(assignment, task=task, route=route, parent_grant=parent_grant)
                try:
                    report = record_codex_result_client(args.state_root, args.project_root, task, route, assignment,
                        parent_grant, result, observation, host_client=host)
                    # The result is committed; the project's learning cycle follows and never fails it.
                    report["learning_cycle"] = learning_cycle_client(
                        args.state_root, args.project_root, host_client=host, full=args.full_report)
                except RuntimeResultError as error:
                    # The run is this host's (checked above), so no published dispatch means it never ran.
                    if derived_route and error.reason_code in {
                            'history_result_not_ready', 'dispatch_not_found', 'dispatch_not_authorized'}:
                        raise LedgerError("a bridged run must be dispatched before its result") from None
                    raise
                _print_command_report(args, report)
                return 0
            if args.route is not None:
                raise RuntimeResultError('history_invalid_input')
            ledger, project_id = _learning_ledger(args)
            try:
                if trusted_host:
                    _require_host_owner(
                        ledger, project_id, run_handle=observation["run_handle"]
                    )
                    if args.evaluation_assignment_digest is not None:
                        raise ContractError(
                            "trusted host handles do not accept evaluation overrides"
                        )
                    route, _ = _trusted_host_route(
                        ledger,
                        project_id,
                        args.project_root,
                        task,
                        fallback_receipt=args.fallback_receipt,
                    )
                    assignment = validate_assignment(
                        _json_input(args.assignment),
                        task=task,
                        route=route,
                        parent_grant=_json_input(args.parent_grant),
                    )
                    admitted = ledger.host_run_admission(
                        project_id, observation["run_handle"]
                    )
                    if admitted["provider"] != route.get("provider", "codex"):
                        raise ContractError("host run admission differs from its route")
                    trusted_now_ms = time.time_ns() // 1_000_000
                    committed = ledger.commit_host_result(
                        project_id,
                        assignment=assignment,
                        task=task,
                        route=route,
                        result=result,
                        observation=observation,
                        execution_configuration=route["execution_configuration"],
                        now_ms=trusted_now_ms,
                    )
                    report = {"ready": True, **committed}
                    report["learning_cycle"] = ledger.run_learning_cycle(
                        project_id, as_of_ms=trusted_now_ms
                    )
                    _print_command_report(args, report)
                    return 0
                policy = load_policy(
                    ORCHESTRATION_ROOT / "config" / "routing-policy.json"
                )
                if args.fallback_receipt is not None:
                    raise ContractError(
                        "fallback receipts require a trusted host run handle"
                    )
                route = route_task(
                    task, policy, primary_provider=_host_primary_provider()
                )
                current_configuration = installed_execution_configuration(
                    BUNDLE_ROOT,
                    route,
                    client_capability=clients[client_version],
                )
                task_slice = routing_task_slice(task)
                route["execution_configuration"] = current_configuration
                if args.evaluation_assignment_digest is not None:
                    evaluation_assignment = ledger.record_by_digest(
                        project_id,
                        args.evaluation_assignment_digest,
                        "evaluation_assignment",
                    )
                    planned = [
                        item for item in evaluation_assignment["trial_plan"]
                        if item["trial_id"] == args.trial_id
                    ]
                    if len(planned) != 1:
                        raise ContractError("trial is not in the evaluation assignment")
                    case = ledger.evaluation_case(project_id, planned[0]["case_id"])
                    candidate, _ = ledger.lifecycle_records(
                        project_id, evaluation_assignment["candidate_digest"]
                    )
                    validate_runtime_candidate(candidate, policy)
                    if (
                        case["task_slice"] != task_slice
                        or task["id"] != case["record_id"]
                        or case["fixture_digest"]
                        != runtime_fixture_digest(ledger.state.digest_key(), task)
                        or evaluation_assignment["client_digest"]
                        != content_digest(clients[client_version])
                        or candidate["champion_configuration"] != current_configuration
                    ):
                        raise ContractError(
                            "evaluation trial does not match installed task and client"
                        )
                    selected = (
                        candidate["candidate_configuration"]
                        if planned[0]["cohort"] == "candidate"
                        else candidate["champion_configuration"]
                    )
                    selected_reference = (
                        candidate["candidate_digest"]
                        if planned[0]["cohort"] == "candidate"
                        else candidate["base_digest"]
                    )
                    if planned[0]["configuration_digest"] != selected_reference:
                        raise ContractError("evaluation trial configuration is invalid")
                    expected_role = policy["roles"].get(task["family"])
                    if selected["role"] != expected_role:
                        raise ContractError("evaluation trial role differs from policy")
                    verifier_model, verifier_banded = verifier_setting(
                        policy, "codex", task["risk"]
                    )
                    verifier = (
                        {
                            "role": policy["verification"]["role"],
                            **verifier_model,
                            "sandbox_mode": "read-only",
                            **({"risk_band": task["risk"]} if verifier_banded else {}),
                        }
                        if expected_role != "codex_agents_verification_worker" else None
                    )
                    if (
                        selected["verification_required"]
                        != (expected_role != "codex_agents_verification_worker")
                        or selected["verifier_digest"]
                        != (content_digest(verifier) if verifier is not None else None)
                        or selected["bounds_digest"] != content_digest(route["bounds"])
                    ):
                        raise ContractError(
                            "evaluation execution controls differ from installed policy"
                        )
                    route.update(
                        model=selected["model"],
                        effort=selected["effort"],
                        role=expected_role,
                        topology=selected["topology"],
                        max_parallel=selected["max_parallel"],
                        verification_required=(
                            expected_role != "codex_agents_verification_worker"
                        ),
                        verifier=verifier,
                        bounds=dict(policy["bounds"]),
                        execution_configuration=selected,
                    )
                else:
                    released = load_released_candidate_digests(
                        RELEASES_PATH,
                        verify_signature=lambda payload, signature: OwnerGrantAuthority.for_state(ledger.state).verify_release_signature(payload, signature),
                    )
                    for candidate in ledger.routing_candidates(
                        project_id,
                        model_selection_slice(task),
                        released_candidate_digests=released,
                    ):
                        route = apply_candidate_to_route(
                            route, candidate, route["execution_configuration"], policy
                        )
                assignment = validate_assignment(
                    _json_input(args.assignment),
                    task=task,
                    route=route,
                    parent_grant=_json_input(args.parent_grant),
                )
                trusted_now_ms = time.time_ns() // 1_000_000
                committed = ledger.commit_host_result(
                    project_id,
                    assignment=assignment,
                    task=task,
                    route=route,
                    result=result,
                    observation=observation,
                    execution_configuration=route["execution_configuration"],
                    now_ms=trusted_now_ms,
                )
                report = {
                    "ready": True,
                    **committed,
                }
                report["learning_cycle"] = ledger.run_learning_cycle(
                    project_id, as_of_ms=trusted_now_ms
                )
            finally:
                _close_ledger(ledger)
        elif args.command in {"learning-run-operational-checks", "learning-record-operational-qa"} and _host_qa(args):
            # A worker and verifier this project's host recorded: QA through the project API.
            try:
                if args.command == "learning-run-operational-checks":
                    report = operational_checks_client(
                        args.state_root, args.project_root, args.host_outcome_digest,
                        args.verifier_host_outcome_digest,
                        lambda diagnostics: run_operational_check_suite(
                            args.project_root, scratch_root=CollectorPaths(args.state_root).root,
                            diagnostics=diagnostics),
                        host_client=_host_client())
                else:
                    report = record_operational_qa_client(
                        args.state_root, args.project_root, args.host_outcome_digest,
                        args.verifier_host_outcome_digest, args.check_receipt,
                        _json_input(args.evaluator_assignment), _json_input(args.evaluator_result),
                        _json_input(args.verifier_assignment), _json_input(args.verifier_result),
                        host_client=_host_client())
                    report["learning_cycle"] = learning_cycle_client(
                        args.state_root, args.project_root, host_client=_host_client(), full=args.full_report)
            except (HostOutcomeUnknown, HistoryError, DispatchError) as error:
                # The project refused the pair, the files or the receipt; nothing was committed.
                _print({"ready": False, "error": "operational QA admission rejected",
                        "reason_code": getattr(error, "reason_code", "history_not_authorized")})
                return 2
        elif args.command == "learning-run-operational-checks":
            ledger, project_id = _learning_ledger(args)
            try:
                ledger.validate_operational_qa_scope(
                    project_id,
                    host_outcome_digest=args.host_outcome_digest,
                    verifier_host_outcome_digest=args.verifier_host_outcome_digest,
                )
                diagnostics: list[dict[str, object]] = []
                check_report = run_operational_check_suite(
                    args.project_root, scratch_root=ledger.state.root, diagnostics=diagnostics
                )
                receipt = ledger.issue_operational_check_receipt(
                    project_id,
                    host_outcome_digest=args.host_outcome_digest,
                    verifier_host_outcome_digest=args.verifier_host_outcome_digest,
                    check_report=check_report,
                )
                report = {
                    "ready": True,
                    **receipt,
                    "check_report": check_report,
                    "diagnostics": diagnostics,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-operational-qa":
            ledger, project_id = _learning_ledger(args)
            try:
                committed = ledger.commit_operational_qa(
                    project_id,
                    host_outcome_digest=args.host_outcome_digest,
                    verifier_host_outcome_digest=args.verifier_host_outcome_digest,
                    evaluator_assignment=_json_input(args.evaluator_assignment),
                    evaluator_result=_json_input(args.evaluator_result),
                    check_receipt=args.check_receipt,
                    verifier_assignment=_json_input(args.verifier_assignment),
                    verifier_result=_json_input(args.verifier_result),
                )
                report = {"ready": True, **committed}
                report["learning_cycle"] = ledger.run_learning_cycle(
                    project_id, as_of_ms=time.time_ns() // 1_000_000
                )
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-verified-outcome":
            ledger, project_id = _learning_ledger(args)
            try:
                inputs = {
                    "evaluation_assignment_digest": args.evaluation_assignment_digest,
                    "host_outcome_digest": args.host_outcome_digest,
                    "verifier_host_outcome_digest": args.verifier_host_outcome_digest,
                    "evaluator_assignment": _json_input(args.evaluator_assignment),
                    "evaluator_result": _json_input(args.evaluator_result),
                    "check_report": _bounded_json_input(args.check_report),
                    "verifier_assignment": _json_input(args.verifier_assignment),
                    "verifier_result": _json_input(args.verifier_result),
                }
                if args.signature is None:
                    qa, outcome = ledger.prepare_verified_evaluation_outcome(
                        project_id,
                        **inputs,
                    )
                    authority = OwnerGrantAuthority.for_state(ledger.state)
                    report = {
                        "ready": True,
                        "qa_evidence": qa,
                        "outcome": outcome,
                        "signing_payload_b64": base64.b64encode(
                            authority.qa_attestation_payload(project_id, qa)
                        ).decode("ascii"),
                        "namespace": "codex-agents-owner-v1",
                    }
                else:
                    report = {
                        "ready": True,
                        **ledger.commit_verified_evaluation_outcome(
                            project_id,
                            **inputs,
                            signature=args.signature.read_bytes(),
                        ),
                    }
            finally:
                _close_ledger(ledger)
        elif args.command in _MOVED_LEARNING_COMMANDS and _learning_moved(args):
            report = _moved_learning_command(args)
            if not report["ready"]:
                _print(report)
                return 2
        elif args.command == "learning-expire":
            ledger, project_id = _learning_ledger(args)
            try:
                trusted_now_ms = time.time_ns() // 1_000_000
                if args.now_ms is not None and args.now_ms > trusted_now_ms:
                    raise ContractError("retention time cannot be in the future")
                report = {
                    "ready": True,
                    "expired_records": ledger.expire(
                        project_id,
                        now_ms=args.now_ms if args.now_ms is not None else trusted_now_ms,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-migrate-project":
            report = _migrate_learning(args)
            if not report["ready"]:
                _print(report)
                return 2
        elif args.command == "learning-run-cycle":
            if args.max_age_ms < 1:
                raise ContractError("max age must be positive")
            ledger, project_id = _learning_ledger(args)
            try:
                trusted_now_ms = time.time_ns() // 1_000_000
                report = {
                    "ready": True,
                    **ledger.run_learning_cycle(
                        project_id,
                        as_of_ms=trusted_now_ms,
                        max_age_ms=args.max_age_ms,
                    ),
                    "expired_records": ledger.expire(
                        project_id, now_ms=trusted_now_ms
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-create-evaluation-assignment":
            case_ids = _json_input(args.case_ids)
            if not isinstance(case_ids, list) or not all(
                isinstance(item, str) for item in case_ids
            ):
                raise ContractError("evaluation case ids must be an array of identifiers")
            ledger, project_id = _learning_ledger(args)
            try:
                candidate, _ = ledger.lifecycle_records(
                    project_id, args.candidate_digest
                )
                validate_runtime_candidate(
                    candidate,
                    load_policy(
                        ORCHESTRATION_ROOT / "config" / "routing-policy.json"
                    ),
                )
                cases = [ledger.evaluation_case(project_id, case_id) for case_id in case_ids]
                clients = load_client_capabilities(
                    ORCHESTRATION_ROOT / "config" / "learning-clients.json"
                )
                if args.client_version not in clients:
                    raise ContractError(
                        "client version is not in the validated capability matrix"
                    )
                record = create_evaluation_assignment(
                    candidate=candidate,
                    cases=cases,
                    client_version=args.client_version,
                    client_capability=clients[args.client_version],
                    observed_at_ms=args.observed_at_ms,
                )
                report = {
                    "ready": True,
                    **ledger.append(
                        project_id, record, producer="evaluator",
                        now_ms=args.observed_at_ms,
                    ),
                    "record": record,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-trial":
            ledger, project_id = _learning_ledger(args)
            try:
                assignment_record = ledger.record_by_digest(
                    project_id,
                    args.evaluation_assignment_digest,
                    "evaluation_assignment",
                )
                planned = [
                    item for item in assignment_record["trial_plan"]
                    if item["trial_id"] == args.trial_id
                ]
                if len(planned) != 1:
                    raise ContractError("trial is not in the evaluation assignment")
                case = ledger.evaluation_case(project_id, planned[0]["case_id"])
                outcome = ledger.record_by_digest(
                    project_id, args.outcome_digest, "outcome"
                )
                record = build_evaluation_trial(
                    case=case,
                    outcome=outcome,
                    assignment=assignment_record,
                    trial_id=args.trial_id,
                )
                report = {
                    "ready": True,
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-evidence-review":
            subjects = _json_input(args.subject_digests)
            if not isinstance(subjects, list) or not all(
                isinstance(item, str) for item in subjects
            ):
                raise ContractError("review subjects must be an array of digests")
            ledger, project_id = _learning_ledger(args)
            try:
                report = {
                    "ready": True,
                    **ledger.commit_evidence_review(
                        project_id, subject_digests=subjects
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-comparison":
            ledger, project_id = _learning_ledger(args)
            try:
                assignment_record = ledger.record_by_digest(
                    project_id,
                    args.evaluation_assignment_digest,
                    "evaluation_assignment",
                )
                candidate, _ = ledger.lifecycle_records(
                    project_id, assignment_record["candidate_digest"]
                )
                cases = [
                    ledger.evaluation_case(project_id, case_id)
                    for case_id in assignment_record["case_ids"]
                ]
                with ledger.repository(project_id) as repository:
                    trials = [
                        {
                            key: value for key, value in record.items()
                            if not key.startswith("_")
                        }
                        for record in repository.records("evaluation_trial")
                        if record["evaluation_assignment_digest"]
                        == args.evaluation_assignment_digest
                    ]
                calibration = (
                    ledger.record_by_digest(
                        project_id,
                        args.grader_calibration_digest,
                        "grader_calibration",
                    )
                    if args.grader_calibration_digest else None
                )
                clients = load_client_capabilities(
                    ORCHESTRATION_ROOT / "config" / "learning-clients.json"
                )
                client_version = assignment_record["client_version"]
                record = compare_candidate(
                    candidate=candidate,
                    trials=trials,
                    cases=cases,
                    client_version=client_version,
                    client_capability=clients[client_version],
                    independent_review_digest=args.independent_review_digest,
                    evaluation_assignment_digest=args.evaluation_assignment_digest,
                    grader_calibration=calibration,
                    subjective_grading_required=args.subjective_grading_required,
                )
                report = {
                    "ready": True,
                    "decision_ready": record["decision"] == "promote",
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-mine-case":
            ledger, project_id = _learning_ledger(args)
            try:
                with ledger.repository(project_id) as repository:
                    outcome = repository.record(args.source_outcome_id, "outcome")
                record = mine_evaluation_case(
                    digest_key=ledger.state.digest_key(),
                    outcome=outcome,
                    fixture_digest=runtime_fixture_digest(
                        ledger.state.digest_key(), _json_input(args.task)
                    ),
                    criteria_digest=args.criteria_digest,
                    lineage_digest=args.lineage_digest,
                )
                report = {
                    "ready": True,
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-review-case":
            ledger, project_id = _learning_ledger(args)
            try:
                with ledger.repository(project_id) as repository:
                    source_case = repository.record(args.case_id, "evaluation_case")
                record = review_evaluation_case(
                    source_case,
                    digest_key=ledger.state.digest_key(),
                    review_digest=args.review_digest,
                    similarity_group_digest=args.similarity_group_digest,
                    destination=args.destination,
                    freeze=args.freeze,
                )
                report = {
                    "ready": True,
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                }
                report["learning_cycle"] = ledger.run_learning_cycle(
                    project_id, as_of_ms=time.time_ns() // 1_000_000
                )
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-analyze":
            ledger, project_id = _learning_ledger(args)
            try:
                with ledger.repository(project_id) as repository:
                    outcomes = repository.records("outcome")
                    feedback = repository.records("feedback")
                clean = [
                    {key: value for key, value in record.items() if not key.startswith("_")}
                    for record in outcomes
                ]
                invalidated = {
                    record["run_id"] for record in feedback
                    if record["label"] in {"rejected", "corrected"}
                }
                report = {
                    "ready": True,
                    "analysis": analyze_outcomes(
                        clean,
                        as_of_ms=args.as_of_ms,
                        max_age_ms=args.max_age_ms,
                        owner_invalidated_run_ids=invalidated,
                    ),
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-generate-candidate":
            spec = _bounded_json_input(args.spec)
            required = {
                "scope", "task_slice", "target", "effect", "base_digest",
                "change_manifest", "champion_configuration",
            }
            if set(spec) != required:
                raise ContractError("candidate spec must use the closed field set")
            ledger, project_id = _learning_ledger(args)
            try:
                with ledger.repository(project_id) as repository:
                    outcomes = repository.records("outcome")
                    feedback = repository.records("feedback")
                invalidated = {
                    record["run_id"] for record in feedback
                    if record["label"] in {"rejected", "corrected"}
                }
                provisional = generate_candidate(
                    digest_key=ledger.state.digest_key(),
                    **spec,
                    evidence_digests=[content_digest(spec)],
                )
                configuration_fields = tuple(
                    provisional["champion_configuration"].keys()
                )

                def execution_configuration(record: dict[str, Any]) -> dict[str, Any]:
                    return {field: record[field] for field in configuration_fields}

                clean = [
                    {key: value for key, value in record.items() if not key.startswith("_")}
                    for record in outcomes
                    if record["task_slice"] == spec["task_slice"]
                    and record["run_id"] not in invalidated
                    and record["accepted"] is True
                    and record.get("execution_state") == "completed"
                    and record.get("admission_state") == "accepted"
                    and record.get("semantic_verdict")
                    in {"pass", "ready", "ready-with-corrections"}
                    and record.get("retry_attempt") is not None
                    and record["verification_required"] is True
                    and record["verifier_digest"] is not None
                    and record["verifier_disagreement"] is False
                    and record["deterministic_pass"] is True
                    and record["security_pass"] is True
                    and record["required_criteria_passed"]
                    == record["required_criteria_total"]
                    and record["coverage"] == "end_to_end_complete"
                ]
                if any(
                    any(
                        record["usage"][field] is None
                        or record.get("usage_sources", {}).get(field, {}).get("source")
                        == "unavailable"
                        or record.get("usage_sources", {}).get(field, {}).get("coverage")
                        != "end_to_end_complete"
                        for field in {
                            "input_tokens", "cached_tokens", "output_tokens",
                            "reasoning_tokens", "model_calls", "tool_calls",
                            "latency_ms",
                        }
                    )
                    for record in clean
                ):
                    raise ContractError(
                        "candidate requires complete authoritative workflow telemetry"
                    )
                champion_by_fixture: dict[str, list[dict[str, Any]]] = {}
                challenger_by_fixture: dict[str, list[dict[str, Any]]] = {}
                for outcome in clean:
                    configuration = execution_configuration(outcome)
                    cohort = None
                    if configuration == provisional["champion_configuration"]:
                        cohort = champion_by_fixture
                    elif configuration == provisional["candidate_configuration"]:
                        cohort = challenger_by_fixture
                    if cohort is not None:
                        cohort.setdefault(outcome["fixture_digest"], []).append(outcome)
                fixtures = sorted(
                    champion_by_fixture.keys() & challenger_by_fixture.keys()
                )[-32:]
                if len(fixtures) < 20:
                    raise ContractError(
                        "candidate requires at least 20 exact matched champion/candidate fixtures"
                    )
                selected: list[dict[str, Any]] = []
                for fixture in fixtures:
                    selected.extend(
                        (
                            champion_by_fixture[fixture][-1],
                            challenger_by_fixture[fixture][-1],
                        )
                    )
                if len({item["assignment_id"] for item in selected}) != len(selected):
                    raise ContractError(
                        "candidate requires 40 distinct retry-deduplicated outcomes"
                    )
                as_of_ms = max(record["observed_at_ms"] for record in selected)
                analysis = analyze_outcomes(
                    selected,
                    as_of_ms=as_of_ms,
                    max_age_ms=2**63 - 1,
                    owner_invalidated_run_ids=invalidated,
                )
                evidence_digests = sorted(content_digest(item) for item in selected)
                record = generate_candidate(
                    digest_key=ledger.state.digest_key(),
                    **spec,
                    evidence_digests=evidence_digests,
                    eligibility_receipt={
                        "task_slice": spec["task_slice"],
                        "evidence_set_digest": content_digest(evidence_digests),
                        "independent_case_count": len(fixtures),
                        "usage_complete": True,
                        "version_unconfounded": True,
                        "task_mix_matched": True,
                        "retry_deduplicated": True,
                        "verifier_evidence_complete": True,
                    },
                )
                report = {
                    "ready": True,
                    **ledger.append(project_id, record, producer="evaluator"),
                    "record": record,
                    "analysis": analysis,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-calibrate-grader":
            labels = _json_input(args.labels)
            configuration = _json_input(args.configuration)
            if not isinstance(labels, list) or not isinstance(configuration, dict):
                raise ContractError("grader inputs must be an array and an object")
            ledger, project_id = _learning_ledger(args)
            try:
                record = calibrate_grader(
                    labels,
                    configuration=configuration,
                    observed_at_ms=args.observed_at_ms,
                )
                authority = OwnerGrantAuthority.for_state(ledger.state)
                payload = authority.owner_record_payload(project_id, record)
                if args.signature is None:
                    report = {
                        "ready": True,
                        "requires_signature": True,
                        "record": record,
                        "signing_payload_b64": base64.b64encode(payload).decode("ascii"),
                    }
                else:
                    report = {
                        "ready": record["ready"],
                        **ledger.commit_owner_record(
                            project_id,
                            record=record,
                            signature=args.signature.read_bytes(),
                        ),
                        "record": record,
                    }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-feedback":
            correction = (
                args.correction_file.read_bytes() if args.correction_file is not None else None
            )
            ledger, project_id = _learning_ledger(args)
            try:
                record = build_feedback(
                    digest_key=ledger.state.digest_key(),
                    run_id=args.run_id,
                    label=args.label,
                    reason_code=args.reason_code,
                    correction=correction,
                    observed_at_ms=args.observed_at_ms,
                )
                authority = OwnerGrantAuthority.for_state(ledger.state)
                payload = authority.owner_record_payload(project_id, record)
                if args.signature is None:
                    report = {
                        "ready": True,
                        "requires_signature": True,
                        "record": record,
                        "signing_payload_b64": base64.b64encode(payload).decode("ascii"),
                    }
                else:
                    report = {
                        "ready": True,
                        **ledger.commit_owner_record(
                            project_id,
                            record=record,
                            signature=args.signature.read_bytes(),
                        ),
                        "record": record,
                    }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-evaluate-candidate":
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                comparison = ledger.record_by_digest(
                    project_id, args.comparison_digest, "comparison"
                )
                transition = lifecycle.mark_evaluated(
                    comparison,
                    evidence_digest=args.comparison_digest,
                    now_ms=args.observed_at_ms,
                )
                report = {
                    "ready": True,
                    **ledger.append(
                        project_id, transition, producer="lifecycle",
                        now_ms=args.observed_at_ms,
                    ),
                    "record": transition,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-start-canary":
            case_digests = _json_input(args.case_digests)
            if not isinstance(case_digests, list):
                raise ContractError("canary case digests must be an array")
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                trusted_now_ms = time.time_ns() // 1_000_000
                transition = lifecycle.start_canary(
                    grant=_bounded_json_input(args.grant),
                    case_digests=case_digests,
                    evidence_digest=args.comparison_digest,
                    now_ms=trusted_now_ms,
                    commit=lambda grant, promotion: ledger.commit_granted_promotion(
                        project_id,
                        grant=grant,
                        promotion=promotion,
                        now_ms=trusted_now_ms,
                    ),
                )
                report = {"ready": True, "record": transition}
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-complete-canary":
            case_results = _json_input(args.case_results)
            if not isinstance(case_results, list):
                raise ContractError("canary case results must be an array")
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                record = build_canary_report(
                    candidate_digest=lifecycle.candidate["candidate_digest"],
                    champion_digest=lifecycle.champion_digest,
                    case_results=case_results,
                    comparison_digest=args.comparison_digest,
                    independent_review_digest=args.independent_review_digest,
                    observed_at_ms=args.observed_at_ms,
                )
                ledger.append(
                    project_id, record, producer="evaluator", now_ms=args.observed_at_ms
                )
                transition = lifecycle.complete_canary(
                    report=record, now_ms=args.observed_at_ms
                )
                ledger.append(
                    project_id,
                    transition,
                    producer="lifecycle",
                    now_ms=args.observed_at_ms,
                )
                report = {
                    "ready": transition["action"] == "canary_passed",
                    "canary_report": record,
                    "record": transition,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-promote":
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                trusted_now_ms = time.time_ns() // 1_000_000
                transition = lifecycle.promote(
                    grant=_bounded_json_input(args.grant),
                    evidence_digest=args.canary_report_digest,
                    now_ms=trusted_now_ms,
                    commit=lambda grant, promotion: ledger.commit_granted_promotion(
                        project_id,
                        grant=grant,
                        promotion=promotion,
                        now_ms=trusted_now_ms,
                    ),
                )
                report = {"ready": True, "record": transition}
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-record-drift":
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                comparison = ledger.record_by_digest(
                    project_id, args.comparison_digest, "comparison"
                )
                transition = lifecycle.record_drift(
                    comparison=comparison,
                    evidence_digest=args.comparison_digest,
                    now_ms=args.observed_at_ms,
                )
                report = {
                    "ready": True,
                    **ledger.append(
                        project_id, transition, producer="lifecycle",
                        now_ms=args.observed_at_ms,
                    ),
                    "record": transition,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-rollback":
            ledger, project_id = _learning_ledger(args)
            try:
                lifecycle = _restore_lifecycle(
                    ledger, project_id, args.candidate_digest
                )
                transition = lifecycle.rollback(
                    active_digest=args.candidate_digest,
                    evidence_digest=args.evidence_digest,
                    now_ms=args.observed_at_ms,
                )
                report = {
                    "ready": True,
                    **ledger.append(
                        project_id, transition, producer="lifecycle",
                        now_ms=args.observed_at_ms,
                    ),
                    "record": transition,
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-configure-owner-key":
            state = LearningState(args.state_root)
            path = state.install_owner_public_key(args.public_key.read_text())
            report = {"ready": True, "allowed_signers": path.name}
        elif args.command == "learning-replace-owner-key":
            ledger, _project_id = _learning_ledger(args)
            try:
                owner_key = ledger.replace_owner_key(args.public_key.read_text())
                report = {"ready": owner_key["state"] == "ok", "owner_key": owner_key}
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-create-grant-request":
            ledger, project_id = _learning_ledger(args)
            try:
                authority = OwnerGrantAuthority.for_state(ledger.state)
                request = authority.request(
                    digest_key=ledger.state.digest_key(),
                    kind=args.kind,
                    project_id=project_id,
                    candidate_digest_value=args.candidate_digest,
                    champion_digest=args.champion_digest,
                    review_digest=args.review_digest,
                    bounds_digest=args.bounds_digest,
                    issued_at_ms=args.issued_at_ms,
                    expires_at_ms=args.expires_at_ms,
                )
                report = {
                    "ready": True,
                    "request": request,
                    "signing_payload_b64": base64.b64encode(
                        authority.signing_payload(request)
                    ).decode("ascii"),
                    "namespace": "codex-agents-owner-v1",
                }
            finally:
                _close_ledger(ledger)
        elif args.command == "learning-attach-grant-signature":
            state = LearningState(args.state_root, create=False)
            authority = OwnerGrantAuthority.for_state(state)
            request = _bounded_json_input(args.request)
            record = authority.attach_signature(
                request, args.signature.read_bytes()
            )
            report = {"ready": True, "record": record}
        elif args.command == "host-command":
            try:
                status = codex_rule_status(args.project_root)
            except CodexHomeUnsupported as exc:
                # The rule and its copy live only in the real ~/.codex.
                report = {
                    "ready": False, "argv_prefix": None, "command_prefix": None,
                    "next_command": None, "codex_rule": "unsupported",
                    "reason": str(exc), "rule_file": None, "runtime_copy": None,
                }
            else:
                try:
                    # The rule runs the protected copy, never the project's own CLI.
                    prefix = codex_argv_prefix(args.project_root)
                    runtime_copy = str(runtime_copy_root(args.project_root))
                except HostCommandError:
                    # No install manifest: nothing to copy, so the rule is stale.
                    prefix = runtime_copy = None
                clone = None
                if runtime_copy is not None:
                    try:
                        clone = Path(
                            runtime_binding(Path(runtime_copy) / ".codex" / "orchestration")
                            ["clone_root"]
                        )
                    except (HostCommandError, TypeError):
                        clone = None  # no copy yet: nothing recorded to protect
                # Text to run as printed: the rule does not match quoted command words.
                runnable = status == "installed" and prefix is not None
                report = {
                    "ready": status == "installed",
                    "argv_prefix": prefix,
                    "command_prefix": shell_command(prefix) if runnable else None,
                    "next_command": shell_command([
                        *prefix, "learning-status",
                        "--project-root", str(project_cli(args.project_root)[0]),
                    ]) if runnable else None,
                    "codex_rule": status,
                    "rule_file": str(codex_rule_path(args.project_root)),
                    "runtime_copy": runtime_copy,
                    # Whether this session would pass the guard the copy applies.
                    "session_guard": session_guard_status(
                        Path(runtime_copy) if runtime_copy is not None else None, clone,
                        Path(os.path.abspath(args.project_root)),
                    ),
                }
        elif args.command == "install-codex-rule":
            rule_file = str(codex_rule_path(args.project_root))
            if args.print_only:
                report = {
                    "ready": True,
                    "rule_file": rule_file,
                    "rule_text": render_codex_rule(args.project_root),
                    "runtime_copy": str(runtime_copy_root(args.project_root)),
                }
            else:
                installed = install_codex_rule(
                    args.project_root, replace=args.replace,
                    clone_root=installer_root(ORCHESTRATION_ROOT),
                )
                report = {
                    "ready": installed["codex_rule"] != "differs",
                    **installed,
                    "rule_file": rule_file,
                }
        elif args.command == "remove-codex-rule":
            report = {"ready": True, "removed": remove_codex_rule(args.project_root)}
        elif args.command == "codex-session-scan":
            report = {
                "ready": True,
                "warnings": scan_recent_sessions(
                    args.clone_root,
                    configured_codex_home(),
                    Path(os.environ.get("HOME") or Path.home()),
                    projects=registered_project_roots(),
                ),
            }
        else:  # pragma: no cover - argparse restricts commands.
            raise ContractError("unsupported command")
    except OSError:
        _print(_with_codex_hint(
            args.command, {"ready": False, "error": "filesystem operation failed"}
        ))
        return 2
    except ProjectServiceOtherVersion:
        _print({
            "ready": False,
            "error": "this project's service was started by another version and a child run is still active "
                     "or unsettled; try again when none is running",
            "reason_code": "project_service_other_version",
        })
        return 2
    except HostClientMismatch:
        _print({
            "ready": False,
            "error": "host client does not own this lifecycle",
            "reason_code": "host_client_mismatch",
        })
        return 2
    except ClaudeHostDenied as exc:
        _print({
            "ready": False,
            "error": "provider admission denied",
            "reason_code": exc.reason_code,
        })
        return 2
    except HostClientUnknown:
        _print({
            "ready": False,
            "error": "host client is unknown",
            "reason_code": "host_client_unknown",
        })
        return 2
    except DispatchError as exc:
        error = "dispatch unavailable"
        if exc.reason_code == "result_capacity_full":
            error = ("the project's result store is full: close finished orchestrations "
                     "(learning-close-orchestration), or wait for their results to pass their retention")
        elif exc.reason_code == "result_capacity_busy":  # O10 RC-1
            error = ("the space for results is held by children still running in this project: "
                     "wait for them to finish, then dispatch again")
        _print({"ready": False, "error": error, "reason_code": exc.reason_code})
        return 2
    except RuntimeOpenError as exc:
        _print({'ready': False, 'error': 'host open unavailable', 'reason_code': exc.reason_code})
        return 2
    except RuntimeOrchestrationError as exc:
        _print({'ready': False, 'error': 'orchestration operation unavailable', 'reason_code': exc.reason_code,
                **({'request_id': exc.request_id} if exc.request_id is not None else {}),
                **({'expected_sequence': exc.expected_sequence} if exc.expected_sequence is not None else {})})
        return 2
    except RuntimeResultError as exc:
        _print({'ready': False, 'error': 'result recording unavailable', 'reason_code': exc.reason_code})
        return 2
    except RuntimeBillingProbeError as exc:
        _print({'ready': False, 'error': 'billing probe unavailable', 'reason_code': exc.reason_code,
                **({'request_id': exc.request_id} if exc.request_id is not None else {}),
                **({'hint': BILLING_ATTESTATION_HINT} if exc.reason_code == 'billing_attestation_missing' else {})})
        return 2
    except RuntimeObservationError as exc:
        _print({'ready': False, 'error': 'provider observation unavailable', 'reason_code': exc.reason_code,
                **({'request_id': exc.request_id} if exc.request_id is not None else {})})
        return 2
    except BridgeRefused as exc:
        # Refusal messages are fixed strings; they never carry prompts or paths.
        report = {"ready": False, "error": "dispatch refused", "reason": str(exc)}
        if str(exc) == "launch denied: billing_attestation_missing":
            report["hint"] = BILLING_ATTESTATION_HINT
        _print(report)
        return 2
    except AdmissionDenied as exc:
        report = {
            "ready": False,
            "error": "provider admission denied",
            "reason_code": exc.reason_code,
        }
        if exc.reason_code == "billing_attestation_missing":
            report["hint"] = BILLING_ATTESTATION_HINT
        _print(report)
        return 2
    except ContractError as exc:
        # Only the field's name is printed: the message can hold the input's values.
        report = {"ready": False, "error": "input validation failed"}
        if exc.field is not None:
            report["field"] = exc.field
        _print(report)
        return 2
    except (
        CatalogError,
        CollectorError,
        HostCommandError,
        LedgerError,
        PolicyError,
        StateError,
        json.JSONDecodeError,
    ) as exc:
        _print(_with_codex_hint(args.command, {"ready": False, "error": str(exc)}))
        return 2
    _print_command_report(args, report)
    successful = report.get("ready", True)
    if args.command == "validate" and args.package_only:
        successful = report.get("package_ready", False)
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
