from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import ORCHESTRATION_ROOT

from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.owner_record_payload import owner_record_payload

VECTORS = Path(__file__).parent / "fixtures/learning/owner-record-payload-vectors.json"


class OwnerRecordPayloadTests(unittest.TestCase):
    def test_exact_pre_extraction_bytes_for_all_existing_record_kinds(self):
        vectors = json.loads(VECTORS.read_text())["vectors"]
        self.assertEqual({"feedback", "grader_calibration", "legacy_billing", "credits_disabled_billing"},
                         {item["name"] for item in vectors})
        for item in vectors:
            with self.subTest(vector=item["name"]):
                before = copy.deepcopy(item["record"])
                expected = item["payload_ascii"].encode("ascii")
                self.assertEqual(expected, owner_record_payload(item["project_id"], item["record"]))
                self.assertEqual(expected, OwnerGrantAuthority.owner_record_payload(
                    item["project_id"], item["record"]))
                self.assertEqual(before, item["record"])

    def test_fresh_import_and_calculation_use_only_metadata_contracts(self):
        # This is an import/behavior check, not an OS containment claim.
        script = """
import json
import sys
sys.path.insert(0, sys.argv[1])
allowed = {
    'codex_agents.contracts', 'codex_agents.learning_contracts',
    'codex_agents.owner_record_payload',
}
class MetadataImportsOnly:
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.partition('.')[0] in {'sqlite3', '_sqlite3'}
                or (fullname.startswith('codex_agents.') and fullname not in allowed)):
            raise AssertionError('unexpected persistence/runtime import: ' + fullname)
sys.meta_path.insert(0, MetadataImportsOnly())
from codex_agents.owner_record_payload import owner_record_payload
vectors = json.load(sys.stdin)['vectors']
def refuse_io(event, arguments):
    if event in {
        'open', 'os.listdir', 'os.scandir', 'os.mkdir', 'os.remove', 'os.rename',
        'os.system', 'os.fork', 'os.posix_spawn', 'os.exec',
        'subprocess.Popen', 'socket.__new__', 'sqlite3.connect',
    }:
        raise AssertionError('unexpected calculation I/O: ' + event)
sys.addaudithook(refuse_io)
for item in vectors:
    assert owner_record_payload(item['project_id'], item['record']) == item['payload_ascii'].encode('ascii')
loaded = {name for name in sys.modules if name.startswith('codex_agents.')}
assert loaded == allowed, loaded
assert not any(name.partition('.')[0] in {'sqlite3', '_sqlite3'} for name in sys.modules)
print('owner-record-payload-isolated')
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c", script, str(ORCHESTRATION_ROOT)],
            input=VECTORS.read_text(), text=True, capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("owner-record-payload-isolated\n", completed.stdout)
