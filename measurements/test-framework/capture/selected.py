"""Profile only the fifteen heaviest baseline modules through the unchanged runner."""
import json
import os
from pathlib import Path
import sys

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / 'scripts'))
import parallel_tests

timings = json.loads(Path('/private/tmp/cocolo-testfw-baseline-timings.json').read_text())
stages = json.loads(parallel_tests.STAGES.read_text())
top = sorted(((key, timings[key]) for key in stages if key in timings), key=lambda row: -row[1])[:15]
out = Path('/private/tmp/cocolo-testfw-profile')
out.mkdir(exist_ok=True)
os.environ['TF_PROFILE_DIR'] = str(out)
parallel_tests._RUNNER = "import sys; sys.path.insert(0, '/private/tmp'); import cocolo_tf_probe\n" + parallel_tests._RUNNER
Path('/private/tmp/cocolo-testfw-top15.json').write_text(json.dumps(top, indent=1) + '\n')
args = ['--save-timings', '/private/tmp/cocolo-testfw-profile-timings.json']
for module, _ in top:
    args += ['--module', module]
raise SystemExit(parallel_tests.main(args))
