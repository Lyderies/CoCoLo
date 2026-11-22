import json
from collections import defaultdict
from pathlib import Path

root = Path.cwd()
stages = json.loads((root / 'scripts/test-stages.json').read_text())
baseline = json.loads(Path('/private/tmp/cocolo-testfw-baseline-timings.json').read_text())
profile_times = json.loads(Path('/private/tmp/cocolo-testfw-profile-timings.json').read_text())
top = json.loads(Path('/private/tmp/cocolo-testfw-top15.json').read_text())
summaries = {}
for key, seconds in top:
    summaries[key] = {'baseline_job_seconds': seconds, 'profile_job_seconds': profile_times[key],
                      'workers': 0, 'counts': defaultdict(int), 'calls': {},
                      'phases': defaultdict(float), 'main_thread_functions': {}}
workers = []
for path in sorted(Path('/private/tmp/cocolo-testfw-profile').glob('*.json')):
    data = json.loads(path.read_text())
    suite = {'codex-agents': 'codex', 'claude-agents': 'claude'}.get(Path(data['cwd']).name, 'root')
    name = data['names'][0]
    matched = [key for key, _ in top if key.startswith(suite + ':') and
               (name == key.split(':', 1)[1] or name.startswith(key.split(':', 1)[1] + '.'))]
    if len(matched) != 1:
        raise ValueError((path, name, matched))
    row = summaries[matched[0]]
    row['workers'] += 1
    for field in ('counts', 'phases'):
        for k, v in data[field].items():
            row[field][k] += v
    for k, v in data['calls'].items():
        output = row['calls'].setdefault(k, {'count': 0, 'seconds': 0.})
        for metric, value in v.items():
            output[metric] += value
    for function in data['functions']:
        label = function['function']
        if any(token in label for token in ('/subprocess.py:', ':setUp', ':tearDown', ':settle', ':expect',
                                             ':wait_for', 'fsync', 'sleep', 'select', 'acquire', ':_reap', 'sqlite')):
            output = row['main_thread_functions'].setdefault(label, {'calls': 0, 'self_seconds': 0., 'inclusive_seconds': 0.})
            for metric in output:
                output[metric] += function[metric]
    workers.append({k: v for k, v in data.items() if k not in ('functions', 'cwd') } | {'module': matched[0]})
out = {'baseline_modules': len(stages),
       'baseline_job_seconds': round(sum(baseline[key] for key in stages), 2),
       'baseline_by_stage': {stage: round(sum(baseline[key] for key in stages if stages[key] == stage), 2)
                             for stage in sorted(set(stages.values()))},
       'top15_job_seconds': round(sum(seconds for _, seconds in top), 2),
       'top15': summaries, 'workers': workers}
Path('/private/tmp/cocolo-testfw-profile-summary.json').write_text(json.dumps(out, indent=1) + '\n')
print(json.dumps({k:v for k,v in out.items() if k not in ('top15', 'workers')}, indent=1))
for key, row in summaries.items():
    print(key, row['baseline_job_seconds'], row['profile_job_seconds'], row['counts'],
          {k: {m: round(v, 2) for m,v in value.items()} for k,value in row['calls'].items()},
          {k:round(v,2) for k,v in row['phases'].items()})
