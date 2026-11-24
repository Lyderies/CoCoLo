"""B5a validation publishes aggregate results without private raw logs."""
from pathlib import Path
import re
import unittest


class ValidationPrivacyTests(unittest.TestCase):
    def test_validation_has_no_raw_log_links_or_files(self):
        directory = Path(__file__).resolve().parents[3] / 'docs' / 'requirements' / 'b5a'
        text = (directory / 'validation.md').read_text()
        self.assertEqual([], re.findall(r'\]\((?:red-[^)]*\.txt|green\.json)\)', text))
        self.assertEqual([], sorted(path.name for path in directory.glob('red-*.txt')))
        self.assertFalse((directory / 'green.json').exists())
