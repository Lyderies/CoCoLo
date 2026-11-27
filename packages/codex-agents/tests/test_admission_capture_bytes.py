"""S14a: lossless admission capture at the sourced doubled peak."""
import runpy
import sqlite3
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import REPOSITORY_ROOT
from test_framework import requirements
from tests import test_admission_history_growth as history
from codex_agents.admission_inputs import AdmissionCaptureError, AdmissionCaptureLimits

DAY_MS, HOUR_MS, MIB = 86_400_000, 3_600_000, 1_048_576

def sizing_tool():
    return runpy.run_path(str(REPOSITORY_ROOT / "scripts/admission_capture_size.py"))

class AdmissionCaptureBytesTests(unittest.TestCase):
    """S14 / U2: the sizing tool's rows and bytes equal what the real job charges, and its margin is reported.

    `scripts/admission_capture_size.py` (the owner's sizing tool, brought to the account host's limits and
    built on the runtime's own sizing functions) reads a ledger copy and reports what an open or claim job would
    be charged. These tests build synthetic ledgers (never an owner ledger), drive the real
    `SignatureJobIntake.capture_admission` and read the charge it records, and require the tool to agree
    exactly. The bulk measurement (the sourced busiest-day envelope) is `scripts/measure_s14_admission_day.py`;
    this class keeps the boundary regressions only.
    """

    def setUp(self):
        history.AdmissionCaptureLimitTests.setUp(self)
        self.sizing = sizing_tool()
        self.growth = history.AdmissionHistoryGrowthTests("test_coverage_finds_an_index_field_that_differs_from_its_record")
        self.growth.ledger = self.ledger
        self.project = self.open.reference.base.scope.project_id
        self.work = self.open.reference.work

    charged = history.AdmissionCaptureLimitTests.charged
    add = history.AdmissionCaptureLimitTests.add
    fill_to_the_limit = history.AdmissionCaptureLimitTests.fill_to_the_limit
    late_claim = history.AdmissionCaptureLimitTests.late_claim
    outcome = history.AdmissionCaptureLimitTests.outcome
    anchor = history.AdmissionCaptureLimitTests.anchor

    def copies(self, kind, count, *, age_ms, change=None):
        return history.AdmissionHistoryGrowthTests.copies(self.growth, kind, count, age_ms=age_ms, change=change)

    def observed(self, anchor_ms=None):
        """What the real job charges: the intake's own capture, (rows, bytes, the oracle's bytes).

        `SignatureJobIntake.capture_admission` is driven as an open or claim job drives it, and the rows and
        bytes it recorded for the job are read back (the handle is released at once). The oracle is the
        original field-by-field formula over the captured snapshot, independent of every sizing function.
        """
        intake = self.jobs._intake
        handle = intake.capture_admission(self.project, "codex", self.work, anchor_ms)
        try:
            job = intake._jobs[handle]
            admission, signing = job.admission, job.inputs
            return (len(admission.rows) + len(signing.rows), admission.retained_bytes + signing.retained_bytes,
                    self.oracle(admission))
        finally:
            intake._release(handle)

    def oracle(self, snapshot):
        """The job's bytes by the original formula: 32 and the stored lengths for each row, the header and
        shared project/type/source copies once per snapshot; signer once for the capture
        and again for the signature worker, each billing
        attestation again as 8 and its lengths."""
        header = len(snapshot.project_id) + len(snapshot.provider)
        owner = 0 if snapshot.owner is None else 16 + len(snapshot.owner[1]) + len(snapshot.owner[2])
        signer = 0 if snapshot.signer is None else len(snapshot.signer)
        total = (header + len(snapshot.runtime_digest) + 24 + len(snapshot.policy.minimum_version)
                 + len(snapshot.policy.maximum_exclusive_version) + owner + signer)  # held by the capture
        total += header + 8 + owner + signer  # and by the signature worker's input
        shared = set()
        for _, project, identifier, _, kind, content, digest, source, _, _, signature in snapshot.rows:
            total += 32 + len(identifier) + len(content) + len(digest)
            for value in (project, kind, source):
                if value not in shared:
                    total += len(value.encode("utf-8"))
                    shared.add(value)
            total += 0 if signature is None else len(signature)
            if kind == "billing_attestation" and project == snapshot.project_id:
                total += 8 + len(identifier) + len(content) + len(digest) + len(source)
                total += 0 if signature is None else len(signature)
        return total

    def tool(self, at_ms):
        """The sizing tool's row for this project, from a read-only copy of the ledger, given the job's actual
        metadata: the current public signer's length and the claim policy the job captures with."""
        copy = sqlite3.connect(":memory:")
        self.addCleanup(copy.close)
        self.ledger._connection.backup(copy)
        signer = self.ledger._current_signer_bytes()
        report = self.sizing["size_report"](copy, at_ms, "codex", signer_bytes=len(signer),
                                            policy=self.jobs._views._claim_policy("codex"))
        [row] = [row for row in report if row["project_id"] == self.project]
        return row

    def accepts(self, rows, size, anchor_ms=None):
        """Whether the real intake, configured with exactly these per-job limits, takes the capture."""
        from codex_agents.signature_job_intake import SignatureIntakeLimits
        intake = self.jobs._intake
        with mock.patch.object(intake, "_limits", SignatureIntakeLimits(2, 2, rows, size, 2)):
            try:
                handle = intake.capture_admission(self.project, "codex", self.work, anchor_ms)
            except AdmissionCaptureError as error:
                self.assertEqual("admission_capture_limit", str(error))
                return False
        intake._release(handle)
        return True

    def assert_parity(self, anchor_ms=None, at_ms=None, *, edge=False):
        """Rows and bytes of the tool equal the real job's, at the same instant; with `edge`, the real intake
        also accepts a job limited to exactly these rows and bytes and refuses one row or one byte fewer."""
        at_ms = self.ledger._clock_ms() if at_ms is None else at_ms
        rows, charged, oracle = self.observed(anchor_ms)
        row = self.tool(at_ms)
        self.assertEqual(rows, row["job_combined"]["rows"], f"rows of the tool and the job: {row['bounded_by_rule']}")
        self.assertEqual(charged, row["job_charged_bytes"]["total"], f"bytes of the tool and the job: {row['bounded_by_rule']}")
        self.assertEqual(charged, oracle, "bytes of the job and the original formula")
        if edge:
            self.assertTrue(self.accepts(rows, charged, anchor_ms))
            self.assertFalse(self.accepts(rows, charged - 1, anchor_ms), "one byte fewer must be refused")
            self.assertFalse(self.accepts(rows - 1, charged, anchor_ms), "one row fewer must be refused")
        return rows, charged, row

    @requirements("S14A-3", why_not_scenario='Synthetic ledger and in-process account job; the sizing tool reads a copy of that same ledger, no service or socket.')
    def test_U2_1_the_tool_equals_the_real_jobs_charge_and_follows_a_product_change(self):
        rows, charged, row = self.assert_parity(edge=True)
        self.assertGreater(rows, 10)
        self.assertGreater(row["job_charged_bytes"]["signature_again"], 0)  # the attestation is counted twice
        # The record-free bytes of this fixture's shape (36-byte project, codex, a 100-byte signer, an enrolled
        # owner header of 124): an independent pin, so a change to a sizing function is noticed here and not
        # only by the oracle (which is the original formula, written out in `oracle`).
        self.assertEqual(640, row["job_charged_bytes"]["fixed"])
        self.assertEqual(charged, row["job_charged_bytes"]["fixed"] + row["job_charged_bytes"]["admission"]
                         + row["job_charged_bytes"]["signature_again"])
        self.assertEqual((4096, 2 * MIB), (row["job_combined"]["limit_rows"], row["job_combined"]["limit_bytes"]))
        self.header_follows_the_metadata(row["job_charged_bytes"]["fixed"])
        self.signature_count_mutations_break_parity()

    def header_follows_the_metadata(self, base):
        """The fixed bytes are the runtime's own count for the metadata given: a longer signer moves them by
        exactly the added bytes in both counts (held and signature input); another provider moves them too."""
        copy = sqlite3.connect(":memory:")
        self.addCleanup(copy.close)
        self.ledger._connection.backup(copy)

        def fixed(*args, **options):
            [found] = [r for r in self.sizing["size_report"](copy, self.ledger._clock_ms(), *args, **options)
                       if r["project_id"] == self.project]
            return found["job_charged_bytes"]["fixed"]
        recorded = copy.execute("SELECT length(signer_line) FROM owner_key_history ORDER BY sequence DESC LIMIT 1").fetchone()[0]
        self.assertEqual(base, fixed())  # the defaults (recorded signer, installed policy) give the job's shape
        self.assertEqual(base + 2 * 30, fixed(signer_bytes=recorded + 30))
        self.assertNotEqual(base, fixed("claude"))  # another provider's name and client versions

    def signature_count_mutations_break_parity(self):
        """In-memory product mutations of the intake's own accounting: the job's charge moves and parity,
        which reads that charge, goes red at the bytes assertion. Unmutated it is green again."""
        from codex_agents import signature_job_intake
        real = signature_job_intake.signature_input_size

        def omitted(project, provider, owner, signer, rows):
            return real(project, provider, owner, signer, tuple((*row[:5], None) for row in rows))

        def doubled(project, provider, owner, signer, rows):
            return real(project, provider, owner, signer, rows) + sum(len(row[5] or b"") for row in rows)
        for name, mutation in (("omitted", omitted), ("doubled", doubled)):
            with self.subTest(mutation=name), mock.patch.object(signature_job_intake, "signature_input_size", mutation), \
                    self.assertRaisesRegex(AssertionError, "bytes of the tool and the job"):
                self.assert_parity()
        self.assert_parity()

    @requirements("S14A-3", why_not_scenario='Synthetic ledger and in-process account job; the sizing tool reads a copy of that same ledger, no service or socket.')
    def test_U2_3_records_exactly_at_each_cutoff_and_older_evidence_in_effect_are_charged_alike(self):
        now = self.ledger._clock_ms()
        self.enterContext(mock.patch.object(self.ledger, "_clock_ms", return_value=now))
        before = self.assert_parity(at_ms=now)[2]["bounded_by_rule"]

        def exhausted(record, now):
            record["windows"] = [{"resets_at": now + HOUR_MS, "scope": None, "used_percent": 100.0, "window_minutes": 10080}]

        def claude(record, now):
            record.update(provider="claude", account_fingerprint="d" * 64)

        def claude_pass(record, now):
            record.update(provider="claude", decision="pass", account_fingerprint="c" * 64)
        # Exactly on each cutoff (read: the runtime's `>=`) and one millisecond past it (not read).
        for kind, edge in (("provider_identity", 30 * 60_000), ("admission_decision", 5 * HOUR_MS),
                           ("provider_billing", DAY_MS), ("provider_headroom", DAY_MS)):
            self.copies(kind, 1, age_ms=edge)
            self.copies(kind, 1, age_ms=edge + 1)
        # Older evidence still in effect.
        self.copies("provider_headroom", 1, age_ms=5 * DAY_MS, change=exhausted)  # a window that has not reset
        self.copies("provider_billing", 3, age_ms=10 * DAY_MS, change=claude)  # the latest older Claude reading
        self.copies("provider_headroom", 3, age_ms=10 * DAY_MS, change=claude)
        self.copies("admission_decision", 2, age_ms=30 * DAY_MS, change=claude_pass)  # the first Claude pass
        _, _, row = self.assert_parity(at_ms=now, edge=True)
        after = row["bounded_by_rule"]
        # The row exactly on the cutoff is read; headroom also keeps the one whose window has not reset.
        for rule, added in (("identity_30min", 1), ("decision_5h", 1), ("billing_24h", 1), ("headroom_24h_or_live", 2)):
            self.assertEqual(added, after[rule]["rows"] - before.get(rule, {"rows": 0})["rows"], rule)
        for rule in ("latest_older_claude_reading", "first_claude_pass"):
            self.assertGreaterEqual(after[rule]["rows"], 1, rule)

    @requirements("S14A-1", why_not_scenario="Synthetic ledger and real in-process intake, no service or socket.")
    def test_doubled_peak_four_accounts_and_sixteen_windows_fit_with_twenty_percent_margin(self):
        day = runpy.run_path(str(REPOSITORY_ROOT / "scripts/measure_s14_admission_day.py"))
        envelope = dict(day["ENVELOPES"]["double_peak"], codex_accounts=2, claude_accounts=2, headroom_windows=16)
        day["populate"](self, envelope)
        report = self.tool(self.ledger._clock_ms())
        self.assertLessEqual(report["job_charged_bytes"]["total"], int(0.8 * 2 * MIB))
        self.assertLess(report["job_combined"]["rows"], 4096)
        self.assert_parity(edge=True)
        # Claude and anchored recaptures use the same full candidate set, with
        # their actual policy/header/signature charges.
        for provider in ("codex", "claude"):
            for anchor in (None, self.ledger._clock_ms()):
                handle = self.jobs._intake.capture_admission(self.project, provider, self.work, anchor)
                try:
                    job = self.jobs._intake._jobs[handle]
                    charged = job.admission.retained_bytes + job.inputs.retained_bytes
                    self.assertLessEqual(charged, int(0.8 * 2 * MIB))
                    self.assertEqual(charged, self.oracle(job.admission))
                finally:
                    self.jobs._intake._release(handle)

    @requirements("S14A-2", why_not_scenario="Pure bounded codec and real capture, no service or socket.")
    def test_packed_content_is_lossless_bounded_and_rejects_corruption(self):
        import zlib
        from codex_agents.admission_capture_content import pack_content, unpack_content
        from codex_agents.learning_contracts import MAX_RECORD_BYTES
        for raw in (b"{}", b"x" * MAX_RECORD_BYTES, b"{\"windows\":[]}" * 100):
            packed = pack_content("provider_headroom", raw)
            self.assertEqual(raw, unpack_content(packed))
            self.assertLessEqual(len(packed), len(raw))
            self.assertEqual(raw, pack_content("billing_attestation", raw))
        packed = pack_content("provider_headroom", b"x" * MAX_RECORD_BYTES)
        self.assertLess(len(packed), 1024)
        bound = pack_content("provider_headroom", b"x" * 4096, "bound-record", 123)
        self.assertEqual(b"x" * 4096, unpack_content(bound, "bound-record", 123))
        for identifier, observed in (("other-record", 123), ("bound-record", 124)):
            with self.assertRaises(ValueError):
                unpack_content(bound, identifier, observed)
        for bad in (packed[:-1], packed + b"trailing", packed + packed):
            with self.subTest(bad=bad[-8:]), self.assertRaises(ValueError):
                unpack_content(bad)
        # Over-expansion is bounded even for a small attacker-supplied stream.
        with self.assertRaises(ValueError):
            unpack_content(packed[:4] + zlib.compress(b"x" * (MAX_RECORD_BYTES + 1)))

    @requirements("S14A-2", why_not_scenario="Real captured records and detached pure decision readers, no sockets.")
    def test_every_stored_byte_and_decision_input_survives_packing(self):
        from dataclasses import replace
        from codex_agents.admission_capture_content import unpack_content
        from codex_agents.admission_inputs import admission_input_size
        from codex_agents.claim_admission import prepare_claim_admission, same_admission_inputs
        from codex_agents import admission_metadata
        from tests import test_admission_input_decode as fixture
        case = fixture.AdmissionInputDecodeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        packed = case.snapshot
        expanded, shared = [], {}
        for row in packed.rows:
            for value in (row[1], row[4], row[7]):
                if value in shared:
                    self.assertIs(shared[value], value)
                shared[value] = value
            content = unpack_content(row[5], row[2], row[8])
            stored = case.ledger._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_id=?", (row[1], row[2])).fetchone()[0]
            self.assertEqual(bytes(stored), content)
            expanded.append((*row[:5], content, *row[6:]))
        expanded = tuple(expanded)
        raw = replace(packed, rows=expanded, retained_bytes=admission_input_size(
            packed.project_id, packed.provider, packed.runtime_digest, packed.policy, packed.owner, packed.signer, expanded))
        self.assertLess(packed.retained_bytes, raw.retained_bytes)
        before = prepare_claim_admission(raw, case.limits)
        after = prepare_claim_admission(packed, case.limits)
        self.assertEqual([item.record for item in before.rows], [item.record for item in after.rows])
        for field in ("billing", "headroom", "attestations", "identities", "admissions", "reviewed_versions"):
            self.assertEqual(getattr(before, field), getattr(after, field), field)
        for provider in ("codex", "claude"):
            for record, _ in before.headroom:
                for now in (fixture.NOW - 1, fixture.NOW, fixture.NOW + HOUR_MS, fixture.NOW + DAY_MS):
                    args = provider, record["account_fingerprint"]
                    self.assertEqual(admission_metadata.classify_provider_state(*args, before.headroom, before.billing, now),
                                     admission_metadata.classify_provider_state(*args, after.headroom, after.billing, now))
                    self.assertEqual(admission_metadata.window_start_ms(*args, before.headroom, now),
                                     admission_metadata.window_start_ms(*args, after.headroom, now))
        # Equal but separately allocated metadata must not get the shared-copy discount.
        from codex_agents.admission_input_decode import AdmissionDecodeError, decode_admission_inputs
        cloned = list(packed.rows)
        item = list(cloned[-1])
        item[1] = item[1].encode().decode()
        cloned[-1] = tuple(item)
        self.assertIsNot(cloned[-1][1], packed.rows[-1][1])
        with self.assertRaisesRegex(AdmissionDecodeError, "^admission_decode_invalid_input$") as caught:
            decode_admission_inputs(replace(packed, rows=tuple(cloned)), case.limits)
        self.assertIsNone(caught.exception.__context__)
        # Recapture equality continues to compare the entire packed row, not a summary.
        self.assertTrue(same_admission_inputs(packed, replace(packed, captured_at_ms=packed.captured_at_ms + 1)))
        changed = list(packed.rows)
        item = list(changed[-1])
        item[5] += b"changed"
        changed[-1] = tuple(item)
        self.assertFalse(same_admission_inputs(packed, replace(packed, rows=tuple(changed))))

    @requirements("S14A-3", why_not_scenario="Real intake boundary and synthetic limit fixture, no worker execution or socket.")
    def test_real_two_mib_boundary_still_refuses_one_byte_over(self):
        self.fill_to_the_limit()
        rows, charged, _ = self.assert_parity(edge=True)
        self.assertLessEqual(charged, 2 * MIB)
        self.assertGreater(charged, 2 * MIB - 2)
        self.assertLess(rows, 4096)
        from codex_agents.admission_input_decode import decode_admission_inputs
        handle = self.jobs._intake.capture_admission(self.project, "codex", self.work)
        try:
            snapshot = self.jobs._intake._jobs[handle].admission
            self.assertEqual(len(snapshot.rows), len(decode_admission_inputs(snapshot, AdmissionCaptureLimits(4096, 2 * MIB))))
        finally:
            self.jobs._intake._release(handle)
    @requirements("S14A-2", why_not_scenario="Synthetic bounded decoder input, no process or socket.")
    def test_expanded_byte_bound_is_checked_before_json(self):
        """The former 1024 x 64-KiB case must refuse under a 2-MiB charge.

        Semantic checks are stubbed only here to isolate the aggregate quota;
        the exact/+1 tests below use complete, valid canonical identity records.
        """
        import hashlib
        from dataclasses import replace
        from codex_agents.admission_capture_content import pack_content
        from codex_agents.admission_input_decode import AdmissionDecodeError, decode_admission_inputs
        from codex_agents.admission_inputs import admission_input_size
        from codex_agents.learning_contracts import MAX_RECORD_BYTES
        from tests import test_admission_input_decode as fixture
        case = fixture.AdmissionInputDecodeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        template = case.snapshot.rows[0]
        rows = []
        for number in range(1024):
            identifier = f"expanded-{number}"
            content = pack_content("provider_headroom", b"x" * MAX_RECORD_BYTES, identifier, fixture.NOW)
            rows.append((number + 1, template[1], identifier, number + 1, "provider_headroom", content,
                         hashlib.sha256(b"x" * MAX_RECORD_BYTES).hexdigest(), "provider", fixture.NOW, fixture.NOW, None))
        rows = tuple(rows)
        snapshot = replace(case.snapshot, rows=rows, retained_bytes=admission_input_size(
            case.snapshot.project_id, case.snapshot.provider, case.snapshot.runtime_digest, case.snapshot.policy,
            case.snapshot.owner, case.snapshot.signer, rows))
        self.assertLess(snapshot.retained_bytes, 2 * MIB)
        def parsed(_):
            return {"record_id": f"expanded-{parser.call_count - 1}", "record_type": "provider_headroom",
                    "observed_at_ms": fixture.NOW}
        with mock.patch("codex_agents.admission_input_decode._depth_bounded", return_value=True), \
                mock.patch("codex_agents.admission_input_decode.parse_bounded_json", side_effect=parsed) as parser, \
                mock.patch("codex_agents.admission_input_decode.validate_learning_record", side_effect=lambda record: record), \
                mock.patch("codex_agents.admission_input_decode.canonical_json", return_value=b"x" * MAX_RECORD_BYTES):
            with self.assertRaisesRegex(AdmissionDecodeError, "^admission_decode_limit$"):
                decode_admission_inputs(snapshot, AdmissionCaptureLimits(4096, 2 * MIB))
            self.assertEqual(96, parser.call_count, "the 97th row exceeds 3 x 2 MiB before its JSON is parsed")

    def expanded_fixture(self, extra=0):
        """Valid records totaling exactly 3 * 4096 (+ extra) decoded bytes."""
        import hashlib
        import json
        from dataclasses import replace
        from codex_agents.admission_capture_content import pack_content, unpack_content
        from codex_agents.admission_inputs import admission_input_size
        from codex_agents.learning_contracts import canonical_json, validate_learning_record
        from tests import test_admission_input_decode as fixture
        case = fixture.AdmissionInputDecodeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        template = next(row for row in case.snapshot.rows if row[4] == "provider_identity")
        record = json.loads(unpack_content(template[5], template[2], template[8]))
        records = [dict(record, record_id=f"expanded-{i:04}", client_path="/x") for i in range(9)]
        padding = 3 * 4096 + extra - sum(len(canonical_json(r)) for r in records)
        self.assertGreater(padding, 0)
        for record in records:
            count = min(padding, 1022)
            record["client_path"] += "x" * count
            padding -= count
            validate_learning_record(record)
        self.assertEqual(0, padding)
        raw, packed = [], []
        for i, record in enumerate(records, 1):
            content = canonical_json(record)
            row = (i, template[1], record["record_id"], i, template[4], content,
                   hashlib.sha256(content).hexdigest(), template[7], template[8], template[9], None)
            raw.append(row)
            packed.append((*row[:5], pack_content(row[4], content, row[2], row[8]), *row[6:]))
        snapshot = replace(case.snapshot, rows=tuple(packed), retained_bytes=admission_input_size(
            case.snapshot.project_id, case.snapshot.provider, case.snapshot.runtime_digest, case.snapshot.policy,
            case.snapshot.owner, case.snapshot.signer, packed))
        self.assertEqual(3 * 4096 + extra, sum(len(row[5]) for row in raw))
        self.assertLess(snapshot.retained_bytes, 4096)
        return case, snapshot, raw

    @requirements("S14A-2", why_not_scenario="Pure decoder with valid canonical records, no service or socket.")
    def test_decoded_content_exact_limit_and_one_byte_over_streaming(self):
        from codex_agents import admission_capture_content as codec
        from codex_agents import admission_input_decode as decoder
        limits = AdmissionCaptureLimits(64, 4096)
        for extra in (0, 1):
            with self.subTest(extra=extra):
                _, snapshot, raw = self.expanded_fixture(extra)
                calls = []
                real = codec.zlib.decompressobj
                class Stream:
                    def __init__(self, *args, **kwargs):
                        self.decoder = real(*args, **kwargs)
                    def __getattr__(self, name):
                        return getattr(self.decoder, name)
                    def decompress(self, data, max_length=0):
                        calls.append(max_length)
                        return self.decoder.decompress(data, max_length)
                with mock.patch.object(codec.zlib, "decompressobj", Stream), \
                        mock.patch.object(decoder, "parse_bounded_json", wraps=decoder.parse_bounded_json) as parser:
                    if extra:
                        with self.assertRaisesRegex(decoder.AdmissionDecodeError, "^admission_decode_limit$") as caught:
                            decoder.decode_admission_inputs(snapshot, limits)
                        self.assertIsNone(caught.exception.__context__)
                        self.assertEqual(len(raw) - 1, parser.call_count)
                    else:
                        self.assertEqual(len(raw), len(decoder.decode_admission_inputs(snapshot, limits)))
                remaining = 3 * limits.max_bytes
                self.assertEqual(len(raw), len(calls))
                for length, row in zip(calls, raw, strict=True):
                    self.assertEqual(remaining + 1, length, "stream stops at the first byte over the remaining quota")
                    remaining -= len(row[5])

    @requirements("S14A-2", why_not_scenario="Owner capture over a synthetic ledger, no service or socket.")
    def test_capture_decoded_content_exact_limit_and_one_byte_over_before_compression(self):
        from codex_agents import admission_capture_content as codec
        from codex_agents.account_service import AccountOwnerViews
        for extra in (0, 1):
            with self.subTest(extra=extra):
                case, expected, raw = self.expanded_fixture(extra)
                connection = case.ledger._connection
                connection.execute("DELETE FROM owner_attestations")
                connection.execute("DELETE FROM records")
                connection.executemany(
                    "INSERT INTO records(rowid,project_id,record_id,source_sequence,record_type,content,content_digest,"
                    "source,observed_at_ms,ingested_at_ms,expires_at_ms,late) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
                    [(*row[:10], row[8] + DAY_MS) for row in raw])
                with mock.patch.object(case.ledger, "_clock_ms", return_value=expected.captured_at_ms), \
                        mock.patch.object(codec.zlib, "compressobj", wraps=codec.zlib.compressobj) as compressor:
                    def capture():
                        return AccountOwnerViews(case.ledger).capture_admission_inputs(
                            expected.project_id, expected.provider, expected.policy, AdmissionCaptureLimits(64, 4096), self.work)
                    if extra:
                        with self.assertRaisesRegex(AdmissionCaptureError, "^admission_capture_limit$"):
                            capture()
                        self.assertEqual(0, compressor.call_count, "length preflight precedes compression")
                    else:
                        snapshot = capture()
                        self.assertEqual(expected.rows, snapshot.rows)
                        self.assertEqual(expected.retained_bytes, snapshot.retained_bytes)
                        self.assertEqual(len(raw), compressor.call_count)

    @requirements("S14A-2", why_not_scenario="Synthetic owner capture and detached decode, no service or socket.")
    def test_capture_preflights_before_compression_and_packs_accepted_rows_once(self):
        from codex_agents import admission_capture_content as codec
        from codex_agents.account_service import AccountOwnerViews
        views = AccountOwnerViews(self.ledger)
        policy = self.jobs._views._claim_policy("codex")
        for limits in (AdmissionCaptureLimits(1, 2 * MIB), AdmissionCaptureLimits(4096, 700)):
            with self.subTest(limits=limits), mock.patch.object(codec.zlib, "compressobj", wraps=codec.zlib.compressobj) as compressor:
                with self.assertRaisesRegex(AdmissionCaptureError, "^admission_capture_limit$"):
                    views.capture_admission_inputs(self.project, "codex", policy, limits, self.work)
                self.assertEqual(0, compressor.call_count)
        with mock.patch.object(codec.zlib, "compressobj", wraps=codec.zlib.compressobj) as compressor:
            snapshot = views.capture_admission_inputs(self.project, "codex", policy, AdmissionCaptureLimits(4096, 2 * MIB), self.work)
        self.assertEqual(sum(row[4] != "billing_attestation" for row in snapshot.rows), compressor.call_count)

    @requirements("S14A-2", why_not_scenario="Detached pure decoder over a real captured fixture, no service or socket.")
    def test_decoder_unpacks_each_row_once(self):
        from codex_agents import admission_input_decode as decoder
        _, snapshot, _ = self.expanded_fixture()
        with mock.patch.object(decoder, "unpack_content", wraps=decoder.unpack_content) as unpack:
            self.assertEqual(len(snapshot.rows), len(decoder.decode_admission_inputs(snapshot, AdmissionCaptureLimits(64, 4096))))
        self.assertEqual(len(snapshot.rows), unpack.call_count)
    @requirements("S14A-2", why_not_scenario="Pure recapture comparison with real packed fixture rows, no service or socket.")
    def test_open_and_probe_appended_record_recaptures_remain_exact(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from codex_agents import account_original_open, account_billing_admission
        from codex_agents.learning_contracts import parse_bounded_json
        from codex_agents.admission_capture_content import unpack_content
        from tests import test_admission_input_decode as fixture
        case = fixture.AdmissionInputDecodeTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        current = case.snapshot
        for kind in ("admission_decision", "probe_admission", "provider_headroom", "provider_billing"):
            row = next(r for r in current.rows if r[4] == kind)
            # Local citation scope is independent of the content's provider.
            local = replace(current, project_id=row[1])
            before = replace(local, rows=tuple(r for r in local.rows if r is not row))
            record = parse_bounded_json(unpack_content(row[5], row[2], row[8]))
            self.assertNotEqual(row[5], unpack_content(row[5], row[2], row[8]))
            reservation = SimpleNamespace(project=row[1], created=0, deadline=current.captured_at_ms + 1)
            owner = SimpleNamespace(_capture=lambda *args: local)
            if kind != "admission_decision":
                self.assertEqual(current.captured_at_ms, account_billing_admission.AccountBillingAdmission._fresh(
                    owner, reservation, before, appended=record))
                with self.assertRaisesRegex(account_billing_admission.ProbeAdmissionError, "^probe_conflict$"):
                    account_billing_admission.AccountBillingAdmission._fresh(
                        owner, reservation, before, appended={**record, "observed_at_ms": record["observed_at_ms"] + 1})
            else:
                request = SimpleNamespace(project_id=row[1], opened_at_ms=0,
                    observation_deadline_at_ms=current.captured_at_ms + 1,
                    execution=SimpleNamespace(model="gpt-6.1-sol", worker_expires_at_ms=None))
                views = SimpleNamespace(_claim_policy=lambda provider: current.policy,
                    _capture_admission_rows_locked=lambda *args: local)
                ledger = SimpleNamespace(_current_signer_bytes=lambda: current.signer,
                    _connection=SimpleNamespace(transaction_read_call=lambda limits, call: call()))
                intake = SimpleNamespace(_views=views, _limits=SimpleNamespace(rows_per_job=4096, bytes_per_job=2 * MIB))
                self.assertEqual(current.captured_at_ms, account_original_open._fresh_admission(
                    ledger, request, intake, before, self.work, appended=record))
                with self.assertRaisesRegex(account_original_open.OpenError, "^open_preparation_stale$"):
                    account_original_open._fresh_admission(ledger, request, intake, before, self.work,
                        appended={**record, "observed_at_ms": record["observed_at_ms"] + 1})
