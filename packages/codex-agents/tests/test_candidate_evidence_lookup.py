"""AP slice 4: real candidate checks, VM work, order and previous-release reopen."""
from copy import deepcopy
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest import mock
import zipfile

from test_framework import requirements
from tests.support import REPOSITORY_ROOT
from tests.test_append_lookup import AppendLookupTests
from tests.learning_support import candidate, event_record, verification_outcome
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.learning_store import LEDGER_SCHEMA_VERSION, LearningLedger, LearningState, LedgerError


OLD_LOOKUP = 'SELECT record_type,content,content_digest FROM records WHERE project_id=?'


def former_candidate_check(ledger, project, checked):
    """Pre-slice candidate branch and invalidation rule, independent of replacement SQL."""
    evidence = {
        str(row['content_digest']): (str(row['record_type']), json.loads(bytes(row['content'])))
        for row in ledger._connection.execute(OLD_LOOKUP, (project,)).fetchall()
    }
    if not checked['evidence_digests'] or any(d not in evidence for d in checked['evidence_digests']):
        raise LedgerError('candidate evidence is not durable project evidence')
    fields = set(checked['champion_configuration'])
    if not any(kind == 'outcome' and record['task_slice'] == checked['task_slice']
               and {f: record[f] for f in fields} == checked['champion_configuration']
               for digest in checked['evidence_digests'] for kind, record in [evidence[digest]]):
        raise LedgerError('candidate champion lacks matching durable outcome evidence')
    invalidated = {
        record['run_id']
        for row in ledger._connection.execute(
            "SELECT content FROM records WHERE project_id=? AND record_type='feedback'", (project,))
        if (record := json.loads(bytes(row['content'])))['label'] in {'rejected', 'corrected'}
    }
    bad = {
        str(row['content_digest'])
        for row in ledger._connection.execute(
            "SELECT content,content_digest FROM records WHERE project_id=? AND record_type='outcome'", (project,))
        if json.loads(bytes(row['content']))['run_id'] in invalidated
    }
    if any(d in bad for d in checked['evidence_digests']):
        raise LedgerError('candidate evidence was invalidated by owner feedback')


class CandidateEvidenceLookupTests(AppendLookupTests):
    def put(self, record, project=None):
        """Mixed durable-row fixtures at the evidence validator seam (not public admission)."""
        project = project or self.project
        connection = self.ledger._connection
        sequence = connection.execute(
            'SELECT COALESCE(MAX(source_sequence),0)+1 FROM records WHERE project_id=?', (project,)).fetchone()[0]
        connection.execute(
            'INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,'
            'ingested_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,0)',
            (project, record['record_id'], record['record_type'], 'host', sequence,
             record.get('observed_at_ms', 100), 100, canonical_json(record), content_digest(record)))
        return content_digest(record)

    def mixed(self):
        proposed = candidate()
        champion = verification_outcome('linked-champion')
        champion.update(**proposed['champion_configuration'], task_slice=proposed['task_slice'])
        champion['configuration_digest'] = content_digest(proposed['champion_configuration'])
        other = verification_outcome('linked-other')
        other['task_slice'] = 'another-slice'
        # Deliberately insert in a different order from the linked digest list.
        other_digest = self.put(other)
        event_digest = self.put(event_record('linked-event'))
        champion_digest = self.put(champion)
        foreign = verification_outcome('other-project-only')
        foreign_digest = self.put(foreign, self.store.project_b_id)
        proposed['evidence_digests'] = [champion_digest, event_digest, other_digest]
        return proposed, champion, foreign_digest

    def grow_unrelated(self, count):
        """Synthetic bulk rows: unique unrelated identities, alternating events/outcomes."""
        connection = self.ledger._connection
        have = connection.execute("SELECT COUNT(*) FROM records WHERE record_id LIKE 'unrelated-%'").fetchone()[0]
        top = connection.execute('SELECT MAX(source_sequence) FROM records WHERE project_id=?', (self.project,)).fetchone()[0]
        records = [event_record('bulk-event'), verification_outcome('bulk-outcome')]
        with self.ledger._lock, connection:
            connection.executemany(
                'INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,'
                'ingested_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,0)',
                [(self.project, f'unrelated-{i}', records[i % 2]['record_type'], 'host',
                  top + i - have + 1, i, i, canonical_json(records[i % 2]), f'{i + 100_000:064x}')
                 for i in range(have, count)])

    def candidate_steps(self, proposed):
        count = [0]
        def tick():
            count[0] += 1
            return 0
        self.ledger._connection.set_progress_handler(tick, 1)
        try:
            with self.ledger._lock:
                self.ledger._validate_evidence_links_locked(self.project, proposed)
        finally:
            self.ledger._connection.set_progress_handler(None, 0)
        return count[0]

    @requirements('AP5-2', why_not_scenario='Real account-ledger fixture and SQLite VM progress counter.')
    def test_AP5_2_fixed_links_have_flat_vm_work_as_unrelated_records_grow(self):
        proposed, _, _ = self.mixed()
        self.grow_unrelated(1_000)
        small = self.candidate_steps(proposed)
        old_small = self.steps(OLD_LOOKUP, (self.project,))
        self.grow_unrelated(21_000)
        large = self.candidate_steps(proposed)
        old_large = self.steps(OLD_LOOKUP, (self.project,))
        self.assertGreater(old_large, 5 * old_small, (old_small, old_large))
        self.assertLess(large, 2 * small + 200, (small, large))
        print(f'AP5-2 VM steps: linked {small} -> {large}; former scan {old_small} -> {old_large}')

    @requirements('AP5-3', why_not_scenario='Compare real validator with preserved pre-slice rules on mixed durable rows.')
    def test_AP5_3_mixed_evidence_answers_and_link_order_match_former_scan(self):
        proposed, champion, foreign = self.mixed()
        def answer(check, record):
            try:
                check(self.ledger, self.project, record)
            except LedgerError as exc:
                return str(exc)
            return 'accepted'
        current = lambda ledger, project, record: ledger._validate_evidence_links_locked(project, record)
        missing = 'f' * 64
        cases = [
            ('valid', {}, 'accepted'),
            ('reversed', {'evidence_digests': list(reversed(proposed['evidence_digests']))}, 'accepted'),
            ('empty', {'evidence_digests': []}, 'candidate evidence is not durable project evidence'),
            ('missing', {'evidence_digests': [missing]}, 'candidate evidence is not durable project evidence'),
            ('foreign', {'evidence_digests': [foreign]}, 'candidate evidence is not durable project evidence'),
            ('event-only', {'evidence_digests': [proposed['evidence_digests'][1]]},
             'candidate champion lacks matching durable outcome evidence'),
            ('wrong-slice', {'task_slice': 'wrong-slice'}, 'candidate champion lacks matching durable outcome evidence'),
            ('wrong-champion', {'champion_configuration': {**proposed['champion_configuration'], 'max_parallel': 64}},
             'candidate champion lacks matching durable outcome evidence'),
            ('missing-before-champion', {'evidence_digests': [missing], 'task_slice': 'wrong'},
             'candidate evidence is not durable project evidence'),
        ]
        history = self.ledger._records_for_project(self.project, None)
        for name, changes, expected in cases:
            with self.subTest(case=name):
                record = {**deepcopy(proposed), **changes}
                before = deepcopy(record)
                self.assertEqual(expected, answer(former_candidate_check, record))
                self.assertEqual(expected, answer(current, record))
                self.assertEqual(before, record)
        for label, run, project, expected in [
            ('rejected', champion['run_id'], self.store.project_b_id, 'accepted'),
            ('rejected', 'unrelated-run', self.project, 'accepted'),
            ('accepted', champion['run_id'], self.project, 'accepted'),
            ('corrected', champion['run_id'], self.project, 'candidate evidence was invalidated by owner feedback'),
            ('rejected', champion['run_id'], self.project, 'candidate evidence was invalidated by owner feedback'),
        ]:
            with self.subTest(label=label, run=run, project=project):
                self.put({'record_id': f'feedback-{label}-{run}', 'record_type': 'feedback',
                          'label': label, 'run_id': run}, project)
                self.assertEqual(expected, answer(former_candidate_check, proposed))
                self.assertEqual(expected, answer(current, proposed))
        self.assertEqual(history, [r for r in self.ledger._records_for_project(self.project, None)
                                   if r['record_type'] != 'feedback'])

    @requirements('AP5-3', why_not_scenario='Public append and real verified cohort with former/new evidence checks in paired projects.')
    def test_AP5_3_public_admission_duplicates_and_history_order_match(self):
        first = self.store._candidate_cohort(self.project, now_ms=100)
        second = self.store._candidate_cohort(self.store.project_b_id, now_ms=100)
        self.assertEqual(first, second)
        original = self.ledger._validate_evidence_links_locked
        def former(project, checked):
            if checked['record_type'] == 'candidate':
                return former_candidate_check(self.ledger, project, checked)
            return original(project, checked)
        for _ in range(2):
            with mock.patch.object(self.ledger, '_validate_evidence_links_locked', side_effect=former):
                before = self.ledger.append(self.project, first, producer='evaluator', now_ms=200)
            after = self.ledger.append(self.store.project_b_id, second, producer='evaluator', now_ms=200)
            self.assertEqual(before, after)
        left = self.ledger._records_for_project(self.project, None)
        right = self.ledger._records_for_project(self.store.project_b_id, None)
        self.assertEqual(left, right)
        self.assertEqual(list(range(1, len(left) + 1)), [r['_source_sequence'] for r in left])
        self.assertEqual(first['evidence_digests'], left[-1]['evidence_digests'])

    @requirements('AP3', why_not_scenario='Remove only the additive index and reopen the actual account ledger.')
    def test_AP3_digest_index_is_additive_on_first_open(self):
        self.reopen()
        self.ledger._connection.execute('DROP INDEX IF EXISTS records_project_digest')
        def schema():
            return [tuple(row) for row in self.ledger._connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name<>'records_project_digest' ORDER BY type,name")]
        before = schema()
        version = self.ledger._connection.execute('PRAGMA user_version').fetchone()[0]
        self.reopen()
        self.assertIsNotNone(self.ledger._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='records_project_digest'").fetchone())
        self.assertEqual(version, self.ledger._connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertEqual(before, schema())

    @requirements('AP3', 'AP5-3', why_not_scenario='Real dropped index, normal reopen, and both candidate lookups against the frozen oracle.')
    def test_AP3_reopen_recreates_digest_index_and_preserves_lookup_answers(self):
        proposed, champion, _ = self.mixed()
        for invalidated in (False, True):
            with self.subTest(invalidated=invalidated):
                if invalidated:
                    self.put({'record_id': 'feedback-reopen', 'record_type': 'feedback',
                              'label': 'rejected', 'run_id': champion['run_id']})
                self.ledger._connection.execute('DROP INDEX records_project_digest')
                self.reopen()
                self.assertEqual(invalidated,
                                 self.ledger._candidate_evidence_invalidated_locked(self.project, proposed))
                if invalidated:
                    for check in (lambda: former_candidate_check(self.ledger, self.project, proposed),
                                  lambda: self.ledger._validate_evidence_links_locked(self.project, proposed)):
                        with self.assertRaisesRegex(LedgerError, '^candidate evidence was invalidated by owner feedback$'):
                            check()
                else:
                    former_candidate_check(self.ledger, self.project, proposed)
                    self.ledger._validate_evidence_links_locked(self.project, proposed)

    @requirements('AP5-3', why_not_scenario='Real connection loses its index after open; neither pinned query may leak SQLite errors.')
    def test_AP5_3_missing_digest_index_gives_plain_ledger_error(self):
        proposed, _, _ = self.mixed()
        self.ledger._connection.execute('DROP INDEX records_project_digest')
        for lookup in (self.ledger._validate_evidence_links_locked,
                       self.ledger._candidate_evidence_invalidated_locked):
            with self.subTest(lookup=lookup.__name__):
                with self.assertRaisesRegex(LedgerError, '^candidate evidence lookup is unavailable; reopen the ledger$'):
                    lookup(self.project, proposed)

    @requirements('AP3', why_not_scenario='Simulate unavailable previous source in shallow CI; compatibility check must fail explicitly, never skip.')
    def test_AP3_missing_previous_source_has_actionable_failure(self):
        for error in (subprocess.CalledProcessError(128, ['git', 'archive'], stderr=b'raw git details'),
                      subprocess.TimeoutExpired(['git', 'archive'], 20),
                      FileNotFoundError('git')):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(subprocess, 'run', side_effect=error):
                    with self.assertRaisesRegex(AssertionError,
                                                r'AP3 compatibility needs previous Codex release 0\.8\.61 source commit 782a7eed'):
                        self.test_AP3_indexed_copy_opens_reads_and_appends_with_real_previous_release()

    @requirements('AP3', why_not_scenario='Extract recorded prior release source and reopen an indexed ledger copy in a real subprocess.')
    def test_AP3_indexed_copy_opens_reads_and_appends_with_real_previous_release(self):
        root = REPOSITORY_ROOT.parents[1]
        releases = json.loads((root / 'packages/claude-agents/previous-releases.json').read_text())['releases']
        baseline = next(r for r in releases if r['release'] == '0.8.61')
        current_schema = LEDGER_SCHEMA_VERSION

        def unavailable(release):
            return self.failureException(
                f"AP3 compatibility needs previous Codex release {release['release']} "
                f"source commit {release['source_commit']}; make that Git object available "
                "(fetch release history in shallow CI) and rerun this check")

        def schema_of(release):
            # Decision 348: derive each release's ledger schema from its archived source; never guess.
            try:
                source = subprocess.run(
                    ['git', 'show', f"{release['source_commit']}:packages/codex-agents/native-package/"
                     '.codex/orchestration/codex_agents/collector_contracts.py'],
                    cwd=root, capture_output=True, check=True, timeout=20, text=True).stdout
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                raise unavailable(release) from None
            found = re.findall(r'^LEDGER_SCHEMA_VERSION\s*=\s*(\d+)\b', source, re.MULTILINE)
            if len(found) != 1:
                raise self.failureException(
                    f"AP3 cannot derive the ledger schema of release {release['release']} "
                    f"(source commit {release['source_commit']}): expected exactly one LEDGER_SCHEMA_VERSION")
            return int(found[0])

        try:
            archive = subprocess.run(['git', 'archive', '--format=zip', baseline['source_commit'],
                                      'packages/codex-agents/native-package'], cwd=root,
                                     capture_output=True, check=True, timeout=20)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise unavailable(baseline) from None
        # Decision 362 (supersedes 348's same-schema branch): B4t H1d raises the floor to PACKAGE_VERSION when the
        # current build writes, so every older release, same schema or older, refuses the indexed copy. The newest
        # recorded previous release is the one checked; its schema is derived, never guessed, and never above the current.
        previous = releases[-1]
        self.assertLessEqual(schema_of(previous), current_schema)
        try:
            archive = subprocess.run(['git', 'archive', '--format=zip', previous['source_commit'],
                                      'packages/codex-agents/native-package'], cwd=root,
                                     capture_output=True, check=True, timeout=20)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise unavailable(previous) from None
        extracted = Path(self.store.temporary.name) / 'previous-source'
        with zipfile.ZipFile(io.BytesIO(archive.stdout)) as bundle:
            bundle.extractall(extracted)
        old_bundle = extracted / 'packages/codex-agents/native-package'
        self.assertEqual(previous['release'], (old_bundle / 'VERSION').read_text().strip())
        proposed = self.store._candidate_cohort(self.project, now_ms=100)
        self.ledger.append(self.project, proposed, producer='evaluator', now_ms=200)
        self.append('compat-late', observed_at_ms=1)
        expected = self.ledger._records_for_project(self.project, None)
        version = self.ledger._connection.execute('PRAGMA user_version').fetchone()[0]
        self.ledger.close()
        copied = Path(self.store.temporary.name) / 'indexed-copy'
        shutil.copytree(self.store.state.root, copied)
        self.reopen()
        self.assertEqual(current_schema, version)

        def snapshot():
            return {str(f.relative_to(copied)): hashlib.sha256(f.read_bytes()).hexdigest()
                    for f in sorted(copied.rglob('*')) if f.is_file()}

        before = snapshot()
        refuse = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from codex_agents import learning_store
assert Path(learning_store.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
assert learning_store.PACKAGE_VERSION == sys.argv[3]
learning_store.LearningLedger(learning_store.LearningState(Path(sys.argv[2])))
'''
        result = subprocess.run([sys.executable, '-B', '-I', '-c', refuse,
                                 str(old_bundle / '.codex/orchestration'), str(copied), previous['release']],
                                capture_output=True, text=True, timeout=30,
                                env={k: v for k, v in os.environ.items() if not k.startswith('PYTHON')})
        self.assertNotEqual(0, result.returncode, 'a released build older than the current one must refuse (B4t H1d floor; decision 362)')
        self.assertIn('LedgerError', result.stderr)
        after = snapshot()
        # The old build's writer fence stamps the lock file before it reads the schema version, so
        # collector.lock may change; every ledger and artifact file must stay byte-identical (T-5).
        ledger_files = {k for k in before.keys() | after.keys() if k != 'collector.lock'}
        self.assertIn('learning.sqlite3', ledger_files)
        self.assertEqual({k: before.get(k) for k in ledger_files}, {k: after.get(k) for k in ledger_files},
                         'refusal must leave the ledger store byte-identical (zero writes)')


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(CandidateEvidenceLookupTests(name)
                             for name in loader.getTestCaseNames(CandidateEvidenceLookupTests)
                             if name in CandidateEvidenceLookupTests.__dict__)


if __name__ == '__main__':
    unittest.main()
