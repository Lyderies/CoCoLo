#!/usr/bin/env python3
"""S14 / U2 busiest-day admission capture, at the sourced workload envelope. Never reads an owner ledger.

Run from packages/codex-agents with the test interpreter and PYTHONPATH including the repository's scripts
directory (the test framework lives there):

    PYTHONPATH=../../scripts $PY -B scripts/measure_s14_admission_day.py [--output FILE]

For each envelope it builds a synthetic ledger (the admission tests' fixture), places the day's records, drives
the real `SignatureJobIntake.capture_admission` (the job an open or claim runs) and reads the rows and bytes it
charges, then asks the sizing tool (`admission_capture_size.py`, built on the runtime's sizing functions) for the
same figure. They must be equal. The report gives each envelope's rows, bytes and margin against the account
host's limits (4,096 rows, 2 MiB per job), which limit is nearer, and where the bytes limit binds.

SOURCES (each figure is either recorded in this repository or an assumption stated with its derivation):

runs per day, peak 387 (RECORDED). The busiest recorded day is 2026-11-22: 248 native agent launches in
    NATIVE-LAUNCHES.md (141, 248, 97, 161, 248 on 11-21..11-25; mean 179) plus 139 review directories under
    reviews/ (by modification day: 77, 139, 80, 24, 44 on 11-21..11-25), each a run opened through the account.
    Native launches are used as the proxy for runs per day, as the plan says; the review directories are
    COCOLO-routed runs on top. The envelopes below use 387 and, for the double day, 774; the bytes-limit search
    finds the day size at which the job is refused.
records one run writes (OBSERVED). One real open and claim through the fixture's real job path
    (AdmissionHistoryGrowthTests.late_claim) wrote exactly: 1 admission_decision, 1 provider_identity,
    1 provider_headroom, 1 provider_billing and 1 dispatch_intent record, each expiring in 90 days: one reading
    of each kind and one identity and one decision per run. Hence readings per run = 1.
admission cap per 5-hour window (RECORDED). max_admissions_per_window of the installed routing policy:
    codex 60, claude 40 (config/routing-policy.json), read at run time.
provider reading spacing (RECORDED bound, ASSUMED rate). A Claude billing probe may not repeat within PROBE_COOLDOWN_MS (5 minutes,
    account_billing_admission.py), and each probe writes an identity, a headroom and a billing record
    (billing_probe_contracts.py): at most 288 of each per account in 24 hours, 6 identities in 30 minutes, 60
    probe admissions in 5 hours. The envelopes charge probes at that hard maximum (a bound, not an observed rate);
    "no_probes" is the other end.
headroom windows per record (RECORDED). The fixture's real Codex reading has 2 windows (5-hour and weekly);
    learning_contracts.validate_provider_headroom allows at most 16 (the largest payload).
non-lifecycle admission decisions (RECORDED). None expected (DESIGN-0.3.2-admission.md, rule R3b): zero here.
    The 5-minute probe readings above come on top of the per-run ones.
burst factor (ASSUMPTION). The busiest 5 hours and busiest 30 minutes run at 4x the day's average rate (a day's
    work arrives in clusters: the five recorded days run 97 to 248 launches).
denied attempts per admission (ASSUMPTION). Each pass is accompanied by 2 recorded denials or retries
    (a cooldown and a retry); passes in 5 hours are capped by the admission caps above.
busiest project's share (ASSUMPTION). Half of the runs: its own identities are the only ones its capture reads.
attestations (ASSUMPTION). Each project's billing attestations are read at every age: one a month for each
    provider account over 3 years.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
HOUR, DAY, MIN = 3_600_000, 86_400_000, 60_000
LIMIT_ROWS, LIMIT_BYTES = 4096, 2 * 1_048_576

# Per envelope: the sourced peak, and 2x and 4x it (readings and payload grow with it).
ENVELOPES = {
    # The recorded peak day, one reading per run (observed), the fixture's 2-window headroom reading.
    "base": dict(runs_per_day=387, readings_per_run=1, burst=4, codex_accounts=1, claude_accounts=1, headroom_windows=2,
                 deny_ratio=2, project_share=0.5, attestations_per_account=36),
    # The same day with no probes at all (readings only from the runs themselves): the lower bound of the cadence.
    "no_probes": dict(runs_per_day=387, readings_per_run=1, burst=4, codex_accounts=1, claude_accounts=1, headroom_windows=2,
                      deny_ratio=2, project_share=0.5, attestations_per_account=36, probe_share=0.0),
    # More accounts: two of each provider, so twice the probe readings.
    "four_accounts": dict(runs_per_day=387, readings_per_run=1, burst=4, codex_accounts=2, claude_accounts=2,
                          headroom_windows=2, deny_ratio=2, project_share=0.5, attestations_per_account=36),
    # The largest headroom payload the validator accepts (16 windows) on the recorded peak day.
    "largest_payload": dict(runs_per_day=387, readings_per_run=1, burst=4, codex_accounts=1, claude_accounts=1,
                            headroom_windows=16, deny_ratio=2, project_share=0.5, attestations_per_account=36),
    "combined_double_peak": dict(runs_per_day=774, readings_per_run=1, burst=4, codex_accounts=2, claude_accounts=2,
                                 headroom_windows=16, deny_ratio=2, project_share=0.5, attestations_per_account=36),
    # Twice the recorded peak day.
    "double_peak": dict(runs_per_day=774, readings_per_run=1, burst=4, codex_accounts=1, claude_accounts=1,
                        headroom_windows=2, deny_ratio=2, project_share=0.5, attestations_per_account=36),
}


def scaled(envelope, factor):
    """The envelope with every count multiplied by `factor` (at least 1 of each), for a fast boundary test."""
    out = dict(envelope)
    for key in ("runs_per_day", "attestations_per_account"):
        out[key] = max(1, int(envelope[key] * factor))
    return out


def counts(envelope):
    """The records each rule keeps for the busiest project, from the envelope and the runtime's own constants."""
    from codex_agents.account_billing_admission import PROBE_COOLDOWN_MS
    from codex_agents.learning_store import INSTALLED_ROUTING_POLICY
    from codex_agents.routing import admission_settings, load_policy
    policy = load_policy(INSTALLED_ROUTING_POLICY)
    cap = {provider: admission_settings(policy, provider)["max_admissions_per_window"] for provider in ("codex", "claude")}
    codex, claude = envelope["codex_accounts"], envelope["claude_accounts"]
    share = envelope.get("probe_share", 1.0)  # of the hard maximum probe rate
    probes_day = int(claude * (DAY // PROBE_COOLDOWN_MS) * share)
    reading = envelope["runs_per_day"] * envelope["readings_per_run"] + probes_day
    passes = min(math.ceil(envelope["runs_per_day"] * 5 / 24 * envelope["burst"]), codex * cap["codex"] + claude * cap["claude"])
    return {
        "provider_billing": reading, "provider_headroom": reading,
        "admission_decision": passes * (1 + envelope["deny_ratio"]) + int(claude * (5 * HOUR // PROBE_COOLDOWN_MS) * share),
        "provider_identity": int(claude * (30 * MIN // PROBE_COOLDOWN_MS) * share)
        + math.ceil(envelope["project_share"] * envelope["runs_per_day"] / 48 * envelope["burst"]),
        "billing_attestation": (codex + claude) * envelope["attestations_per_account"],
        "cap_codex": cap["codex"], "cap_claude": cap["claude"]}


def populate(case, envelope, tag=""):
    """Place the envelope's records in the case's ledger, spread evenly through each window (never clustered),
    and return the counts placed. `case` is an admission test with a `ledger`; `tag` keeps a second call's
    record identifiers apart from the first's."""
    from codex_agents.learning_contracts import canonical_json
    ledger = case.ledger
    connection = ledger._connection
    now = ledger._clock_ms()
    wanted = counts(envelope)
    templates = {kind: connection.execute("SELECT project_id,source,content FROM records WHERE record_type=? "
                                          "ORDER BY observed_at_ms DESC LIMIT 1", (kind,)).fetchone()
                 for kind in ("provider_billing", "provider_headroom", "admission_decision", "provider_identity",
                              "billing_attestation")}
    signature = connection.execute("SELECT signature FROM owner_attestations LIMIT 1").fetchone()[0]
    sequence = connection.execute("SELECT MAX(source_sequence) FROM records").fetchone()[0]
    accounts = [("codex", hashlib.sha256(f"codex-account-{index}".encode()).hexdigest()) for index in range(1, envelope["codex_accounts"] + 1)] + \
        [("claude", hashlib.sha256(f"claude-account-{index}".encode()).hexdigest()) for index in range(1, envelope["claude_accounts"] + 1)]
    rows, attestations = [], []
    windows = {"provider_identity": 30 * MIN, "admission_decision": 5 * HOUR, "provider_billing": DAY, "provider_headroom": DAY}
    for kind, window in windows.items():
        count = wanted[kind]
        template = json.loads(bytes(templates[kind]["content"]))
        for index in range(count):
            record = dict(template)
            observed = now - 1 - index * (window - 2) // count  # evenly through the window, inside it
            provider, fingerprint = accounts[index % len(accounts)]
            record.update(observed_at_ms=observed, record_id=f"{kind}:{hashlib.sha256(f'{tag}{kind}-{index}'.encode()).hexdigest()}")
            if kind in ("provider_billing", "provider_headroom", "admission_decision"):
                record.update(provider=provider, account_fingerprint=fingerprint)
            if kind == "provider_headroom":
                base = record["windows"]
                record["windows"] = [dict(base[i % len(base)], window_minutes=base[i % len(base)]["window_minutes"] + i)
                                     for i in range(envelope["headroom_windows"])]
            content = canonical_json(record)
            sequence += 1
            rows.append((templates[kind]["project_id"], record["record_id"], kind, templates[kind]["source"], sequence,
                         observed, observed, observed + 90 * DAY, content, hashlib.sha256(content).hexdigest()))
    template = json.loads(bytes(templates["billing_attestation"]["content"]))
    for index in range(wanted["billing_attestation"]):
        record = dict(template)
        observed = now - 1 - index * DAY  # old attestations are read at every age
        record.update(observed_at_ms=observed, record_id=f"billing_attestation:{hashlib.sha256(f'{tag}att-{index}'.encode()).hexdigest()}")
        content = canonical_json(record)
        sequence += 1
        rows.append((templates["billing_attestation"]["project_id"], record["record_id"], "billing_attestation",
                     templates["billing_attestation"]["source"], sequence, observed, observed, observed + 90 * DAY, content,
                     hashlib.sha256(content).hexdigest()))
        attestations.append((templates["billing_attestation"]["project_id"], record["record_id"], signature, observed))
    connection.executemany(
        "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,"
        "ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,0)", rows)
    connection.executemany("INSERT INTO owner_attestations(project_id,subject_record_id,signature,attested_at_ms) "
                           "VALUES(?,?,?,?)", attestations)
    connection.commit()
    return wanted


def measure(case_class, envelope):
    """Build the envelope on a fresh ledger; the real job's charge, the tool's, and the margins."""
    from codex_agents.admission_inputs import AdmissionCaptureError
    started = time.monotonic()
    case = case_class()
    case.setUp()
    try:
        placed = populate(case, envelope)
        row = case.tool(case.ledger._clock_ms())
        rows, charged = row["job_combined"]["rows"], row["job_charged_bytes"]["total"]
        try:
            observed = case.observed()
        except AdmissionCaptureError as error:
            # The real job refuses an envelope past its limits; the tool must say the same.
            assert str(error) == "admission_capture_limit" and (rows > LIMIT_ROWS or charged > LIMIT_BYTES), (rows, charged)
            observed = None
        else:
            assert observed[:2] == (rows, charged) and observed[2] == charged, (observed, rows, charged)
        by_rule = {rule: {"rows": item["rows"], "charged_bytes": item["charged_bytes"]}
                   for rule, item in row["bounded_by_rule"].items()}
        return {"envelope": envelope, "placed": placed, "job_refused": observed is None, "rows": rows, "bytes": charged,
                "signature_again_bytes": row["job_charged_bytes"]["signature_again"], "fixed_bytes": row["job_charged_bytes"]["fixed"],
                "margin_rows": LIMIT_ROWS - rows, "margin_bytes": LIMIT_BYTES - charged,
                "rows_used_percent": round(100 * rows / LIMIT_ROWS, 1), "bytes_used_percent": round(100 * charged / LIMIT_BYTES, 1),
                "nearer_limit": "bytes" if charged / LIMIT_BYTES > rows / LIMIT_ROWS else "rows",
                "by_rule": by_rule, "seconds": round(time.monotonic() - started, 1)}
    finally:
        case.doCleanups()


RULE = {"provider_billing": "billing_24h", "provider_headroom": "headroom_24h_or_live", "admission_decision": "decision_5h",
        "provider_identity": "identity_30min", "billing_attestation": "attestation_every_age"}


def predicted(result, envelope, runs):
    """The job's bytes at `runs` a day, from the measured average of each family's rows."""
    wanted = counts(dict(envelope, runs_per_day=runs))
    total = result["fixed_bytes"]
    for kind, rule in RULE.items():
        held = result["by_rule"].get(rule, {"rows": 0, "charged_bytes": 0})
        average = held["charged_bytes"] / max(1, held["rows"])
        if kind == "billing_attestation":
            average += result["signature_again_bytes"] / max(1, held["rows"])
        total += wanted[kind] * average
    return total


def bind_search(case_class, name, envelope, result):
    """Runs a day at which a slot limit refuses the real job: predicted from the measured rows, then bracketed by
    two real jobs (the largest day accepted and a day 3 percent larger that is refused)."""
    low, high = 1, 200_000
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if predicted(result, envelope, middle) <= LIMIT_BYTES else (low, middle)
    accepted = low
    while True:
        built = measure(case_class, dict(envelope, runs_per_day=accepted))
        if not built["job_refused"] and built["rows"] <= LIMIT_ROWS:
            break
        accepted = int(accepted * 0.97)
    refused = int(accepted * 1.03) + 1
    while True:
        built_over = measure(case_class, dict(envelope, runs_per_day=refused))
        if built_over["job_refused"]:
            break
        refused = int(refused * 1.05) + 1
    return {"name": name, "accepted_runs_per_day": accepted, "accepted_bytes": built["bytes"], "accepted_rows": built["rows"],
            "refused_runs_per_day": refused, "refused_bytes": built_over["bytes"], "refused_rows": built_over["rows"],
            "nearer_limit_when_refused": built_over["nearer_limit"],
            "times_the_recorded_peak": round(accepted / ENVELOPES["base"]["runs_per_day"], 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from tests import test_admission_capture_bytes as growth

    class Case(growth.AdmissionCaptureBytesTests):
        def runTest(self):
            pass
    results = {name: measure(Case, envelope) for name, envelope in ENVELOPES.items()}
    where_it_binds = [bind_search(Case, name, ENVELOPES[name], results[name]) for name in ("base", "no_probes", "largest_payload")]
    report = {"limits": {"rows": LIMIT_ROWS, "bytes": LIMIT_BYTES}, "envelopes": results, "job_limit_binds": where_it_binds}
    print(json.dumps(report, indent=1))
    if args.output:
        args.output.write_text(json.dumps(report, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
