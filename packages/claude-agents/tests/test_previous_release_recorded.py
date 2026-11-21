"""Guard: a release must not ship without the Claude bundle of the release before it being recorded.

Rule: runtime-releases.json lists the runtime versions; the newest is the one in progress, the
second newest is the last release. previous-releases.json must hold an entry whose "release" is that
last release, so projects on it can upgrade. Normal commits inside the in-progress version never
break it (the newest version needs no entry yet); it fails only when a new version is added to
runtime-releases.json (the release is cut) while the version before it has no recorded bundle.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
RELEASES = PACKAGE.parents[1] / "packages" / "codex-agents" / "runtime-releases.json"


def missing_record(versions, records):
    """The last release's version when previous-releases.json has no entry for it, else None."""
    ordered = sorted(versions, key=lambda v: tuple(int(p) for p in v.split(".")))
    if len(ordered) < 2:
        return None
    last = ordered[-2]
    return None if any(r.get("release") == last for r in records) else last


class PreviousReleaseRecordedTests(unittest.TestCase):
    def test_the_last_release_has_its_claude_bundle_recorded(self) -> None:
        versions = json.loads(RELEASES.read_text())
        records = json.loads((PACKAGE / "previous-releases.json").read_text())["releases"]
        self.assertIsNone(missing_record(versions, records),
                          "add the last release's Claude bundle to previous-releases.json "
                          "(compile it from that release's source) before cutting the next release")

    def test_the_rule_fails_when_a_release_is_cut_without_a_record(self) -> None:
        records = [{"release": "0.8.40"}]
        self.assertIsNone(missing_record(["0.8.40", "0.8.41"], records))
        self.assertEqual("0.8.41", missing_record(["0.8.40", "0.8.41", "0.8.42"], records))
        self.assertIsNone(missing_record(["0.8.40", "0.8.41", "0.8.42"], records + [{"release": "0.8.41"}]))


if __name__ == "__main__":
    unittest.main()
