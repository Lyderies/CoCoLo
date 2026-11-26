#!/usr/bin/env python3
"""How many records the bounded admission capture would read, per project. Read-only.

Adapted from the accepted S14 owner sizing tool for S14a
(tests/test_admission_capture_bytes.py). The open and claim jobs' limits
are 4,096 rows and 2 MiB (account_host.ACCOUNT_INTAKE_LIMITS).

The byte figures are the runtime's own: identical project/type/source strings are retained once per snapshot,
and unsigned content is losslessly packed with the runtime codec. Original-content totals are diagnostic
and are not a lower bound on the retained packed charge. This tool imports `admission_input_size` and `signature_input_size`
from the runtime package (the one beside it, or --runtime-root) and asks them for the charge of each losslessly packed row
(with stand-in values of the retained lengths, never the values) and of the job's fixed header: project,
provider, runtime digest, policy versions, owner generation and signer, counted once by
admission_input_size and again by signature_input_size. A change to either function therefore changes this
tool's figures with it; the test's independent oracle (an independent field-by-field count of retained copies) is what notices.
The job's actual metadata is read from the ledger (owner generation) or named (--signer-bytes, --provider)
and from the installed routing policy (client versions).

Opens a COCOLO ledger read-only (pass --immutable for a saved copy that nothing writes) and
counts, for each project, the rows today's capture reads (every age) and the rows the
0.3.2 rules keep (account_service._capture_admission_rows_locked), with their content bytes, by the rule that
keeps them, and the open or claim job's combined figure against its limits (account_host.ACCOUNT_INTAKE_LIMITS: 4,096 rows, 2 MiB).
Prints counts and byte totals only: no content, identifier, name or path from the records.

Usage: python3 -I -B admission_capture_size.py --ledger <learning.sqlite3> [--immutable] [--at-ms N]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

MIN, HOUR, DAY = 60_000, 3_600_000, 86_400_000
RUNTIME_ROOT = Path(__file__).resolve().parents[1] / "native-package" / ".codex" / "orchestration"


def load_runtime(root=None):
    """The runtime's sizing functions, metadata and limits: (admission_input_size, signature_input_size,
    runtime digest, installed policy for a provider, (rows, bytes) the account host gives a job)."""
    root = Path(root) if root else RUNTIME_ROOT
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from codex_agents.account_host import ACCOUNT_INTAKE_LIMITS
    from codex_agents.admission_inputs import AdmissionInputPolicy, admission_input_size
    from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
    from codex_agents.learning_store import INSTALLED_ROUTING_POLICY
    from codex_agents.routing import admission_settings, load_policy
    from codex_agents.signature_inputs import signature_input_size

    def installed_policy(provider):
        settings = admission_settings(load_policy(INSTALLED_ROUTING_POLICY), provider)
        return AdmissionInputPolicy(settings["client_versions"]["minimum"],
                                    settings["client_versions"]["maximum_exclusive"],
                                    settings.get("max_admissions_per_window"))
    return (admission_input_size, signature_input_size, COLLECTOR_RUNTIME_DIGEST, installed_policy,
            (ACCOUNT_INTAKE_LIMITS.rows_per_job, ACCOUNT_INTAKE_LIMITS.bytes_per_job))


class Sized:
    """A stand-in for a stored value of a known length (the sizing functions read only its length)."""

    def __init__(self, length):
        self._length = length

    def __len__(self):
        return self._length


TODAY = ("((r.project_id=? AND r.record_type IN ('provider_identity','billing_attestation','admission_decision')) "
         "OR (r.source='provider' AND r.record_type IN ('provider_billing','provider_headroom')) "
         "OR (r.source='lifecycle' AND r.record_type IN ('admission_decision','probe_admission')))")


def row_lengths(connection):
    """Per candidate row, the retained lengths the runtime charges for (lengths only, never values):
    {rowid: (project, identifier, kind, content, digest, source, signature or None)}, and the same per
    billing attestation as the signature worker's row: {rowid: (identifier, content, digest, source, signature)}."""
    signature = ("(SELECT length(a.signature) FROM owner_attestations a WHERE a.project_id=r.project_id "
                 "AND a.subject_record_id=r.record_id)")
    try:
        connection.execute("SELECT 1 FROM owner_attestations LIMIT 1")
    except sqlite3.Error:
        signature = "NULL"
    from codex_agents.admission_capture_content import pack_content
    connection.create_function("admission_capture_content", 4, pack_content, deterministic=True)
    rows, attestations = {}, {}
    for rowid, kind, project, identifier, digest, source, content, signed in connection.execute(
            "SELECT r.rowid,r.record_type,length(CAST(r.project_id AS BLOB)),length(CAST(r.record_id AS BLOB)),"
            "length(CAST(r.content_digest AS BLOB)),length(CAST(r.source AS BLOB)),length(admission_capture_content(r.record_type,r.content,r.record_id,r.observed_at_ms)),"
            f"{signature} FROM records r WHERE r.record_type IN ('provider_identity',"
            "'billing_attestation','admission_decision','probe_admission','provider_billing','provider_headroom')"):
        rows[rowid] = (project or 0, identifier or 0, kind, content or 0, digest or 0, source or 0, signed)
        if kind == "billing_attestation":
            attestations[rowid] = (identifier or 0, content or 0, digest or 0, source or 0, signed)
    return rows, attestations


def owner_generation(connection):
    """The latest owner generation as the runtime's capture reads it, with stand-in bytes: or None."""
    try:
        row = connection.execute("SELECT sequence,length(signer_line),reason,recorded_at_ms FROM owner_key_history "
                                 "ORDER BY sequence DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else (row[0], Sized(row[1] or 0), row[2], row[3])


def account_rows(connection):
    """Account-wide candidates, decoded once: (rowid, project, kind, source, observed, bytes, record)."""
    rows = []
    for rowid, project, kind, source, observed, content in connection.execute(
            "SELECT rowid,project_id,record_type,source,observed_at_ms,content FROM records WHERE "
            "record_type IN ('provider_identity','billing_attestation','admission_decision','probe_admission',"
            "'provider_billing','provider_headroom')"):
        try:
            record = json.loads(bytes(content))
        except (ValueError, RecursionError):
            record = {}
        rows.append((rowid, project, kind, source, observed, len(content), record))
    return rows


def kept(rows, project, at, rules=None):
    keep = set()
    rules = {} if rules is None else rules
    bootstrap, old_billing, old_headroom = {}, defaultdict(list), defaultdict(list)
    for rowid, owner, kind, source, observed, _, record in rows:
        local = owner == project
        if kind == "provider_identity" and local and observed >= at - 30 * MIN:
            keep.add(rowid); rules[rowid] = 'identity_30min'                        # R1
        elif kind == "billing_attestation" and local:
            keep.add(rowid); rules[rowid] = 'attestation_every_age'                 # R2
        elif kind in ("admission_decision", "probe_admission") and source == "lifecycle":
            if observed >= at - 5 * HOUR:
                keep.add(rowid); rules[rowid] = 'decision_5h'                       # R3
            fingerprint = record.get("account_fingerprint")
            if record.get("provider") == "claude" and record.get("decision") == "pass" and fingerprint:
                key = (observed, owner, record.get("record_id", ""), rowid)
                bootstrap[fingerprint] = min(bootstrap.get(fingerprint, key), key)  # R4
        elif kind == "admission_decision" and local:
            keep.add(rowid); rules[rowid] = 'non_lifecycle_decision_every_age'      # R3b
        elif kind == "provider_billing" and source == "provider":
            if observed >= at - DAY:
                keep.add(rowid); rules[rowid] = 'billing_24h'                       # R5
            elif record.get("provider") == "claude":
                old_billing[record.get("account_fingerprint")].append((observed, rowid))  # R6
        elif kind == "provider_headroom" and source == "provider":
            windows = record.get("windows") or []
            if observed >= at - DAY or any(isinstance(w, dict) and isinstance(w.get("resets_at"), int)
                                           and w["resets_at"] > at for w in windows):
                keep.add(rowid); rules.setdefault(rowid, 'headroom_24h_or_live')    # R7, R8
            elif record.get("provider") == "claude":
                old_headroom[record.get("account_fingerprint")].append((observed, rowid))  # R9
    for *_, rowid in bootstrap.values():
        keep.add(rowid); rules.setdefault(rowid, 'first_claude_pass')                       # R4
    for group in (*old_billing.values(), *old_headroom.values()):
        newest = max(observed for observed, _ in group)
        for observed, rowid in group:
            if observed == newest:
                keep.add(rowid); rules.setdefault(rowid, 'latest_older_claude_reading')     # R6, R9
    return keep


def size_report(connection, at_ms, provider="codex", *, signer_bytes=None, policy=None, runtime=None):
    """One row per project (in ledger order): what the open or claim job would read and be charged at `at_ms`.

    The charge is the runtime's own: `admission_input_size` for what the capture holds (rows, signatures,
    header) and `signature_input_size` for the second count the signature worker's input adds (header,
    signer and each billing attestation again). `signer_bytes` is the current public signer's length (the
    recorded owner generation's, by default); `policy` the provider's admission client versions (the installed
    routing policy's, by default).
    """
    sizing, signing, digest, installed, (limit_rows, limit_bytes) = runtime if runtime is not None else load_runtime()
    policy = installed(provider) if policy is None else policy
    owner = owner_generation(connection)
    if signer_bytes is None:
        signer_bytes = 0 if owner is None else len(owner[1])
    signer = Sized(signer_bytes) if signer_bytes else None
    projects = [row[0] for row in connection.execute("SELECT project_id FROM projects ORDER BY rowid")]
    rows = account_rows(connection)
    lengths, attestation_lengths = row_lengths(connection)
    sizes = {row[0]: row[5] for row in rows}
    raw_by_id = {row[0]: row for row in rows}
    report = []
    for ordinal, project in enumerate(projects, 1):
        def admission_size(held):
            return sizing(project, provider, digest, policy, owner, signer, held)

        def signature_size(held):
            return signing(project, provider, owner, signer, held)
        header_admission, header_signature = admission_size(()), signature_size(())

        def row_charge(rowid):
            length = lengths[rowid]
            row = (0, "x" * length[0], "x" * length[1], 0, length[2], Sized(length[3]), "d" * length[4],
                   "x" * length[5], 0, 0, None if length[6] is None else Sized(length[6]))
            return admission_size((row,)) - header_admission

        def again_charge(rowid):
            length = attestation_lengths[rowid]
            row = ("x" * length[0], 0, Sized(length[1]), "d" * length[2], "x" * length[3],
                   None if length[4] is None else Sized(length[4]))
            return signature_size((row,)) - header_signature
        today = [row[0] for row in connection.execute(f"SELECT r.rowid FROM records r WHERE {TODAY}", (project,))]
        rules = {}
        bounded = kept(rows, project, at_ms, rules)
        shared, charges = set(), {}
        for rowid in sorted(bounded):
            charge = row_charge(rowid)
            _, owner_project, kind, source, *_ = raw_by_id[rowid]
            for value in (owner_project, kind, source):
                if value in shared:
                    charge -= len(value.encode("utf-8"))
                else:
                    shared.add(value)
            charges[rowid] = charge
        by_rule = defaultdict(lambda: {"rows": 0, "content_bytes": 0, "charged_bytes": 0})
        for rowid in bounded:
            entry = by_rule[rules.get(rowid, "other")]
            entry["rows"] += 1
            entry["content_bytes"] += sizes[rowid]
            entry["charged_bytes"] += charges[rowid]
        attestations = [rowid for rowid in bounded if rules.get(rowid) == "attestation_every_age"]
        # The claim and open jobs (signature_job_intake.capture_admission) also count each of the
        # project's billing attestations a second time, as the signature worker's input.
        combined_rows = len(bounded) + len(attestations)
        combined_bytes = sum(sizes[r] for r in bounded) + sum(sizes[r] for r in attestations)
        admission = sum(charges.values())
        again_bytes = sum(again_charge(r) for r in attestations)
        fixed = header_admission + header_signature
        total = fixed + admission + again_bytes
        report.append({"project": ordinal, "project_id": project,
                       "today": {"rows": len(today), "content_bytes": sum(sizes.get(r, 0) for r in today)},
                       "bounded": {"rows": len(bounded), "content_bytes": sum(sizes[r] for r in bounded)},
                       "bounded_by_rule": dict(sorted(by_rule.items())),
                       "job_combined": {"rows": combined_rows, "original_content_bytes": combined_bytes,
                                        "limit_rows": limit_rows, "limit_bytes": limit_bytes},
                       "job_charged_bytes": {"admission": admission, "signature_again": again_bytes,
                                             "fixed": fixed, "total": total, "limit": limit_bytes,
                                             "margin_bytes": limit_bytes - total,
                                             "margin_rows": limit_rows - combined_rows}})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--immutable", action="store_true", help="for a saved copy nothing writes")
    parser.add_argument("--at-ms", type=int, default=int(time.time() * 1000), help="evaluation time")
    parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    parser.add_argument("--signer-bytes", type=int, help="the current public signer's length (default: the recorded owner's)")
    parser.add_argument("--runtime-root", help="the runtime package folder whose sizing functions are used")
    args = parser.parse_args()
    uri = f"file:{args.ledger}?mode=ro" + ("&immutable=1" if args.immutable else "")
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    try:
        report = size_report(connection, args.at_ms, args.provider, signer_bytes=args.signer_bytes,
                             runtime=load_runtime(args.runtime_root))
    finally:
        connection.rollback()
        connection.close()
    for row in report:
        del row["project_id"]  # counts and byte totals only: no identifier from the records
    print(json.dumps({"evaluated_at_ms": args.at_ms, "projects": report}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
