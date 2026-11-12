from __future__ import annotations

from dataclasses import FrozenInstanceError
import hashlib
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from tests.support import ORCHESTRATION_ROOT
from tests.learning_support import heldout_cases, verification_outcome
from tests.test_learning_cycle_execution import MACOS
from codex_agents import learning_cycle_input as inputs
from codex_agents import learning_cycle_process as helper
from codex_agents.learning_contracts import canonical_json

BINDING = "d" * 64


def entry(record):
    raw = canonical_json(record)
    return record["record_id"], hashlib.sha256(raw).hexdigest(), raw


def typed(outcomes=(), cases=(), invalidated=(), **clocks):
    return inputs.LearningCycleInput(tuple(entry(item) for item in outcomes),
        tuple(entry(item) for item in cases), tuple(invalidated), **clocks)


def wire(value, operation="select_mining"):
    return inputs.encode_learning_input(value, operation=operation, binding_digest=BINDING,
        deadline_ns=time.monotonic_ns() + 10**10, cancelled=threading.Event())


class LearningInputTests(unittest.TestCase):
    def test_exact_wire_parity_for_selection_and_nonempty_plan(self):
        outcome = verification_outcome("outcome-00")
        case = heldout_cases(1)[0]
        for operation, clocks in (("select_mining", {}), ("plan", {"as_of_ms": 2, "max_age_ms": 10})):
            value = typed([outcome], [case], ["雪😀", "\\", '"'], **clocks)
            captured, charge = inputs.capture_learning_input(value, operation=operation)
            expected = canonical_json(dict(schema_version=1, operation=operation,
                binding_digest=BINDING, outcomes=[outcome], cases=[case],
                owner_invalidated_run_ids=list(value.owner_invalidated_run_ids), **clocks))
            raw = wire(captured, operation)
            self.assertEqual(expected, raw)
            self.assertLessEqual(len(raw), charge)
            self.assertEqual(helper.calculate_learning_request(expected, operation=operation),
                             helper.calculate_learning_request(raw, operation=operation))
            self.assertIsNot(value, captured)
            self.assertIs(value.outcomes[0][2], captured.outcomes[0][2])

    def test_record_identity_digest_type_canonical_and_closed_fields(self):
        record = verification_outcome()
        identifier, digest, raw = entry(record)
        bad_records = [
            ("another-id", digest, raw),
            (identifier, "0" * 64, raw),
            (identifier, hashlib.sha256(raw + b" ").hexdigest(), raw + b" "),
            (identifier, hashlib.sha256(b"{}").hexdigest(), b"{}"),
            (identifier, digest, b'{"record_id":"a","record_id":"b"}'),
        ]
        for change in (dict(record_type="evaluation_case"), dict(secret="SYNTHETIC-SECRET"),
                       dict(schema_version=True)):
            changed = canonical_json({**record, **change})
            bad_records.append((identifier, hashlib.sha256(changed).hexdigest(), changed))
        for item in bad_records:
            with self.subTest(kind=item[0]):
                with self.assertRaisesRegex(inputs.LearningInputError, "^learning_input_invalid$") as caught:
                    wire(inputs.LearningCycleInput((item,), ()))
                self.assertNotIn("SYNTHETIC", repr(caught.exception))
        case_entry = entry(heldout_cases(1)[0])
        with self.assertRaises(inputs.LearningInputError):
            wire(inputs.LearningCycleInput((case_entry,), ()))
        with self.assertRaises(inputs.LearningInputError):
            wire(inputs.LearningCycleInput((), (entry(record),)))
        with self.assertRaises(TypeError):
            inputs.LearningCycleInput((), (), digest_key=b"SYNTHETIC-SECRET")
        self.assertNotIn("SYNTHETIC", repr(typed([record])))

    def test_frozen_recapture_mutation_exact_types_and_callback_traps(self):
        class Trap:
            def fail(self, *args):
                raise AssertionError("caller callback")
            __iter__ = __len__ = __eq__ = __hash__ = __repr__ = __str__ = fail
        class TupleTrap(tuple):
            __iter__ = __len__ = Trap.fail
        class BytesTrap(bytes):
            __len__ = Trap.fail
        class StrTrap(str):
            __len__ = __hash__ = Trap.fail
        class InputTrap(inputs.LearningCycleInput):
            def __getattribute__(self, name):
                raise AssertionError("subclass access")
        good_entry = entry(verification_outcome())
        with self.assertRaises(FrozenInstanceError):
            typed().outcomes = ()
        for value in (Trap(), InputTrap((), ()), inputs.LearningCycleInput(TupleTrap(), ())):
            with self.assertRaises(inputs.LearningInputError):
                inputs.capture_learning_input(value, operation="select_mining")
        for item in (Trap(), TupleTrap(good_entry),
                     (StrTrap("id"), good_entry[1], good_entry[2]),
                     ("id", StrTrap("d" * 64), good_entry[2]),
                     ("id", good_entry[1], BytesTrap(b"{}"))):
            with self.assertRaises(inputs.LearningInputError):
                inputs.capture_learning_input(inputs.LearningCycleInput((item,), ()),
                                              operation="select_mining")
        for name in ("outcomes", "cases", "owner_invalidated_run_ids", "as_of_ms", "max_age_ms"):
            value = typed()
            object.__setattr__(value, name, Trap())
            with self.assertRaises(inputs.LearningInputError):
                inputs.capture_learning_input(value, operation="select_mining")
        value = typed()
        object.__delattr__(value, "outcomes")
        with self.assertRaises(inputs.LearningInputError):
            inputs.capture_learning_input(value, operation="select_mining")
        original = typed([verification_outcome()])
        captured, _ = inputs.capture_learning_input(original, operation="select_mining")
        object.__setattr__(original, "outcomes", ())
        self.assertEqual(1, len(captured.outcomes))
        self.assertFalse(hasattr(captured, "__dict__"))

    def test_structural_envelopes_and_unicode_charge(self):
        item = ("id", "d" * 64, b"{}")
        for value in (
            inputs.LearningCycleInput((item, item), ()),
            inputs.LearningCycleInput((), (item, item)),
            inputs.LearningCycleInput((item,) * (inputs.MAX_OUTCOMES + 1), ()),
            inputs.LearningCycleInput((), (item,) * (inputs.MAX_CASES + 1)),
            typed(invalidated=("x",) * (inputs.MAX_INVALIDATED_RUNS + 1)),
            typed(invalidated=("",)), typed(invalidated=("x" * 257,)),
            inputs.LearningCycleInput((("x" * 161, "d" * 64, b"{}"),), ()),
            inputs.LearningCycleInput((("é", "d" * 64, b"{}"),), ()),
            inputs.LearningCycleInput((("id", "D" * 64, b"{}"),), ()),
            inputs.LearningCycleInput((("id", "d" * 64, b""),), ()),
            inputs.LearningCycleInput((("id", "d" * 64, b"x" * (inputs.MAX_RECORD_BYTES + 1)),), ()),
        ):
            with self.assertRaises(inputs.LearningInputError):
                inputs.capture_learning_input(value, operation="select_mining")
        value = typed(invalidated=("雪😀", "\ud800"))
        _, charge = inputs.capture_learning_input(value, operation="select_mining")
        self.assertEqual(1_048_576 + 12 * 3 + 3 * 2, charge)
        blob = b"x" * inputs.MAX_RECORD_BYTES
        entries = tuple((f"id-{i}", "d" * 64, blob) for i in range(1023))
        last = ("last", "d" * 64, blob[:-1024])
        value = inputs.LearningCycleInput(entries + (last,), ())
        _, charge = inputs.capture_learning_input(value, operation="select_mining")
        self.assertEqual(inputs.MAX_INPUT_BYTES, charge)
        for final in (blob[:-1023], blob):
            with self.assertRaises(inputs.LearningInputError):
                inputs.capture_learning_input(inputs.LearningCycleInput(
                    entries + (("last", "d" * 64, final),), ()), operation="select_mining")
        with self.assertRaises(inputs.LearningInputError):
            inputs.capture_learning_input(inputs.LearningCycleInput(
                entries + (("last", "d" * 64, blob), ("overflow", "d" * 64, b"x")), ()),
                operation="select_mining")
        with self.assertRaises(inputs.LearningInputError):
            inputs.capture_learning_input(inputs.LearningCycleInput(
                entries + (last,), (), ("x" * 256,)), operation="select_mining")

    def test_clock_metadata_and_original_stop_samples(self):
        for clock in (None, True, -1, 1 << 63, 1.0):
            for name in ("as_of_ms", "max_age_ms"):
                options = {"as_of_ms": 0, "max_age_ms": 0, name: clock}
                with self.assertRaises(inputs.LearningInputError):
                    inputs.capture_learning_input(typed(**options), operation="plan")
        with self.assertRaises(inputs.LearningInputError):
            inputs.capture_learning_input(typed(as_of_ms=0), operation="select_mining")
        value = typed([verification_outcome()])
        options = dict(operation="select_mining", binding_digest=BINDING,
                       deadline_ns=100, cancelled=threading.Event())
        for ticks, suffix, finished in (
            ([10, 11, 12, 100], "expired", 100),
            ([10, 11, 12, 11], "clock_failure", None),
            ([True], "clock_failure", None),
            ([-1], "clock_failure", None),
            ([1 << 63], "clock_failure", None),
            ([OSError("SYNTHETIC-SECRET")], "clock_failure", None),
        ):
            with mock.patch.object(inputs.time, "monotonic_ns", side_effect=ticks):
                with self.assertRaises(inputs.LearningInputStopped) as caught:
                    inputs.encode_learning_input(value, **options)
            self.assertEqual((suffix, finished), (caught.exception.suffix, caught.exception.finished_ns))
            self.assertNotIn("SYNTHETIC", repr(caught.exception))
        real_validate = inputs.validate_outcome
        def cancel_after_validation(record):
            result = real_validate(record)
            options["cancelled"].set()
            return result
        with mock.patch.object(inputs, "validate_outcome", cancel_after_validation), \
                mock.patch.object(inputs.time, "monotonic_ns", return_value=10):
            with self.assertRaises(inputs.LearningInputStopped) as caught:
                inputs.encode_learning_input(value, **options)
        self.assertEqual(("cancelled", 10), (caught.exception.suffix, caught.exception.finished_ns))
        for ticks in ([100], [10, 11, 100], [10, 11, 12, 13, 100], [10, 11, 12, 13, 14, 100]):
            options["cancelled"].clear()
            with mock.patch.object(inputs.time, "monotonic_ns", side_effect=ticks):
                with self.assertRaises(inputs.LearningInputStopped) as caught:
                    inputs.encode_learning_input(value, **options)
            self.assertEqual("expired", caught.exception.suffix)

    def test_fresh_pure_import_and_strict_no_io_capture_and_encoding(self):
        script = r'''
import sys
sys.path.insert(0, sys.argv[1])
allowed = {"codex_agents.contracts", "codex_agents.learning_contracts",
           "codex_agents.learning_analysis", "codex_agents.learning_cycle_core",
           "codex_agents.learning_cycle_input"}
class Imports:
    def find_spec(self, name, path=None, target=None):
        if (name.partition(".")[0] in {"sqlite3", "_sqlite3", "openai", "anthropic"}
                or name.startswith("codex_agents.") and name not in allowed):
            raise AssertionError("impure dependency")
sys.meta_path.insert(0, Imports())
from codex_agents import learning_cycle_input as i
assert not any(n.startswith("codex_agents.") and n not in allowed for n in sys.modules)
raw = bytes.fromhex(sys.argv[2])
record = i.parse_bounded_json(raw)
value = i.LearningCycleInput(((record["record_id"], i.hashlib.sha256(raw).hexdigest(), raw),), ())
event = i.threading.Event()
deadline = i.time.monotonic_ns() + 10**10
def no_io(event, args):
    if (event == "open" or event.startswith(("os.", "subprocess.", "socket.", "ctypes.",
            "mmap.", "sqlite3.")) or event in {"resource.setrlimit", "resource.prlimit"}):
        raise AssertionError("calculation IO")
sys.addaudithook(no_io)
captured, charge = i.capture_learning_input(value, operation="select_mining")
encoded = i.encode_learning_input(captured, operation="select_mining",
    binding_digest="d" * 64, deadline_ns=deadline, cancelled=event)
assert raw in encoded and len(encoded) <= charge
'''
        completed = subprocess.run([sys.executable, "-I", "-B", "-S", "-c", script,
            str(ORCHESTRATION_ROOT), entry(verification_outcome())[2].hex()],
            capture_output=True, timeout=15)
        self.assertEqual(0, completed.returncode, completed.stderr.decode("ascii", errors="replace"))
        self.assertEqual(b"", completed.stdout)


@unittest.skipUnless(MACOS, 'disposable Darwin helper policy; no native/migration/service acceptance')
class LearningInputOwnerIntegrationTests(unittest.TestCase):
    def setUp(self):
        from tests.test_project_learning_identity import ProjectLearningIdentityTests
        from tests.test_learning_cycle_worker import limits
        from codex_agents import learning_cycle_worker as workers
        self.source = ProjectLearningIdentityTests(methodName='runTest')
        self.addCleanup(self.source.doCleanups)
        self.source.setUp()
        self.pool = workers.LearningWorkerPool(limits(input_bytes=8 * 1_048_576))
        self.pool.bootstrap()
        self.addCleanup(self.close_pool)

    def close_pool(self):
        self.pool.request_close()
        deadline = time.monotonic() + 5
        while not self.pool.snapshot()['closed']:
            self.assertLess(time.monotonic(), deadline, 'owned fixture did not confirm worker shutdown')
            self.pool.poll()
            for handle in tuple(self.pool._jobs):
                if self.pool.state(handle) == 'returned':
                    self.pool.take(handle)
            time.sleep(0.002)

    def result(self, value, operation):
        handle = self.pool.submit(value, operation=operation, binding_digest=BINDING,
                                  deadline_ns=time.monotonic_ns() + 10**10)
        deadline = time.monotonic() + 5
        while self.pool.state(handle) != 'returned':
            self.assertNotEqual('unknown', self.pool.state(handle), 'helper custody unconfirmed')
            self.assertLess(time.monotonic(), deadline)
            self.pool.poll()
            time.sleep(0.002)
        return self.pool.take(handle)

    def owner_entries(self):
        store = self.source.store
        revision = store.learning_outcome_revision()
        page = store.learning_outcome_page(revision)
        return tuple((identity, digest, raw) for sequence, identity, digest, raw in page)

    def test_actual_owner_pages_and_keyed_case_metadata_reach_keyless_helper(self):
        from codex_agents.learning_cycle_core import plan_learning_cycle
        cases = self.source.prepare()
        value = inputs.LearningCycleInput(self.owner_entries(), tuple(entry(case) for case in cases),
                                         as_of_ms=10, max_age_ms=100)
        expected = plan_learning_cycle([self.source.outcome], all_cases=cases, as_of_ms=10, max_age_ms=100)
        result = self.result(value, 'plan')
        self.assertEqual(('learning_execution_completed', 1, False),
                         (result.reason, result.roots_reaped, result.no_process_started))
        self.assertEqual(expected, result.result)
        self.assertTrue(result.result['analysis']['slices'])
        self.assertFalse(self.source.store._connection.in_transaction)
        self.assertEqual(1, self.source.store._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0])
        # Computation grants no corpus activation or persistence of inert cases.

    def test_corrupt_raw_owner_page_is_refused_before_any_helper_is_started(self):
        from codex_agents import learning_cycle_execution as execution
        changed = {**self.source.outcome, 'quality_micros': 500_000}
        self.source.store._connection.execute('UPDATE records SET content=? WHERE record_id=?',
            (canonical_json(changed), self.source.outcome['record_id']))
        value = inputs.LearningCycleInput(self.owner_entries(), ())
        with mock.patch.object(execution, '_execute_learning_job', side_effect=AssertionError('corrupt page launched helper')) as run:
            result = self.result(value, 'select_mining')
            run.assert_not_called()
        self.assertEqual(('learning_execution_invalid_output', None, 0, True),
                         (result.reason, result.result, result.roots_reaped, result.no_process_started))
