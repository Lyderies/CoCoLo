"""S1-H1d: one review's worker, verifier and operational QA as a COCOLO command (PLAN H1d-1 to H1d-5).

The same sequence the primary would otherwise run by hand, each step through this package's own
CLI, each reply saved in the work folder. Worker and verifier must give a verdict (H1d-2).
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

_SPEC_KEYS = {'id', 'summary', 'packet', 'context', 'objective', 'verify_objective', 'expected_output', 'evidence'}


class ReviewError(RuntimeError):
    def __init__(self, reason_code: str, detail: str = '') -> None:
        self.reason_code, self.detail = reason_code, detail
        super().__init__(reason_code)


def check_spec(spec, project_root: Path) -> None:
    """H1d-3: a closed spec whose packet lies inside the project."""
    try:
        ok = (type(spec) is dict and set(spec) == _SPEC_KEYS
              and all(type(spec[k]) is str and spec[k] for k in ('id', 'summary', 'packet', 'objective',
                                                                   'verify_objective', 'expected_output'))
              and type(spec['context']) is list and all(type(item) is str for item in spec['context'])
              and type(spec['evidence']) is list and spec['evidence']
              and all(type(item) is dict and set(item) == {'id', 'description'} for item in spec['evidence']))
        if ok:
            root = Path(project_root).resolve()
            packet = (root / spec['packet']).resolve()
            ok = packet != root and root in packet.parents and packet.is_dir()
    except (OSError, ValueError, TypeError):
        ok = False
    if not ok:
        raise ReviewError('review_invalid_spec')


def project_cli(project_root: Path):
    """The default caller: this project's installed CLI, isolated, one JSON reply per call."""
    def call(name, *args):
        done = subprocess.run([sys.executable, '-I', '-B', '-S', '-X', 'pycache_prefix=/dev/null',
                               '.codex/orchestration/cli.py', name, *args],
                              cwd=project_root, capture_output=True, text=True)
        try:
            return json.loads(done.stdout)
        except ValueError:
            return {'ready': False, 'reason_code': 'review_step_unreadable'}
    return call


def run(step, spec, work_dir, project_root, *, cli=None):
    """Run one stage (`worker`, `verify` or `finish`); returns its summary, or raises ReviewError."""
    project_root = Path(project_root)
    check_spec(spec, project_root)
    cli = cli or project_cli(project_root)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    root = ['--project-root', str(project_root)]
    grant = {'tools': ['shell'], 'filesystem': [f"{spec['packet']}:read"], 'network': False, 'mcps': [],
             'external_effects': False}

    def save(name, data):
        (work / name).write_text(json.dumps(data, indent=1))
        return str(work / name)

    def load(name):
        return json.loads((work / name).read_text())

    def call(out, name, *args):
        reply = cli(name, *args)
        save(out, reply)
        if type(reply) is not dict or reply.get('ready') is False or reply.get('valid') is False:
            code = reply.get('reason_code') if type(reply) is dict else None
            raise ReviewError(code if type(code) is str else 'review_step_refused', name)
        return reply

    def task(task_id, family, objective, extra=None):
        return {'id': task_id, 'objective': objective, 'family': family, 'complexity': 'medium', 'risk': 'high',
                'operation': 'review', 'ambiguity': 'known', 'context_breadth': 'narrow',
                'acceptance_strength': 'independent_judgment', 'routing_evidence_refs': [], 'meaningful': True,
                'deterministic_validation': False, 'independent_parts': 1, 'parallel_safe': False,
                'failed_attempts': 0, 'failure_events': [], 'owned_paths': [spec['packet']], **(extra or {})}

    def assignment(task_id, route, objective, context):
        return {'assignment_id': task_id + '-a1', 'task_id': task_id, 'objective': objective,
                'role': route['agent_type'], 'model': route['execution_configuration']['model'],
                'effort': route['effort'], 'attempt': 1, 'meaningful': True, 'owned_paths': [spec['packet']],
                'context': context, 'permissions': grant, 'expected_output': spec['expected_output'],
                'required_evidence': spec['evidence'], 'verification_required': route['verification_required'],
                'bounds': {'max_attempts': 2, 'max_steers_per_attempt': 1, 'max_no_progress_events': 2,
                           'wait_seconds': 60, 'timeout_seconds': 1800, 'max_tool_calls': 30},
                'stopping_conditions': ['Every requirement is assessed from the packet with line references.'],
                'prohibited_actions': ['Changing any file.']}

    def child(prefix, t, objective, context, orchestration):
        task_file = save(prefix + 'task.json', t)
        route = call(prefix + 'route.json', 'learning-route', '--task', task_file, *root)
        assignment_file = save(prefix + 'assignment.json', assignment(t['id'], route, objective, context))
        grant_file = save('grant.json', grant)
        common = ['--task', task_file, '--assignment', assignment_file, '--parent-grant', grant_file, *root]
        call(prefix + 'validate.json', 'validate-assignment', *common)
        call(prefix + 'preflight.json', 'result-preflight', *common)
        provider = 'claude' if route['agent_type'].startswith('claude') else 'codex'
        call(prefix + 'observe.json', 'provider-observe', '--provider', provider,
             *(['--billing-probe'] if provider == 'claude' else []), *root)
        route_file = str(work / (prefix + 'route.json'))
        opened = call(prefix + 'open.json', 'learning-open-host-run', '--task', task_file, '--route', route_file,
                      '--assignment', assignment_file, '--parent-grant', grant_file,
                      '--orchestration-handle', orchestration, *root)
        dispatched = call(prefix + 'dispatch.json', 'dispatch', '--task', task_file, '--route', route_file,
                          '--assignment', assignment_file, '--parent-grant', grant_file,
                          '--run-handle', opened['run_handle'], *root)
        result_file = save(prefix + 'result.json', dispatched['result'])
        observation = {'run_handle': opened['run_handle'],
                       'result_attempts': [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}]}
        terminal = (dispatched.get('bridge_report') or {}).get('terminal_state')
        if terminal:
            observation['terminal_state'] = terminal
        observation_file = save(prefix + 'observation.json', observation)
        recorded = call(prefix + 'record.json', 'learning-record-host-result', '--task', task_file,
                        '--assignment', assignment_file, '--parent-grant', grant_file, '--result', result_file,
                        '--observation', observation_file, *root)
        verdict = dispatched['result'].get('semantic_verdict')
        if verdict is None:
            raise ReviewError('review_no_verdict', prefix or 'worker')  # H1d-2
        return {'digest': recorded['host_outcome_digest'], 'provider': provider, 'verdict': verdict,
                'summary': dispatched['result'].get('summary')}

    if step == 'worker':
        orchestration = call('orchestration.json', 'learning-open-orchestration', *root)['orchestration_handle']
        worker = child('', task(spec['id'], 'security', spec['summary']), spec['objective'], spec['context'],
                       orchestration)
        save('worker.json', worker)
        return worker
    if step == 'verify':
        orchestration, worker = load('orchestration.json')['orchestration_handle'], load('worker.json')
        verifier = child('v', task(spec['id'] + '-verify', 'verification',
                                   'Independently check the recorded review: ' + spec['summary'],
                                   {'verification_for_family': 'security',
                                    'verification_for_provider': worker['provider'],
                                    'verification_for_outcome': worker['digest']}),
                         spec['verify_objective'], [worker['digest']], orchestration)
        save('verifier.json', verifier)
        return verifier
    if step == 'finish':
        digests = ['--host-outcome-digest', load('worker.json')['digest'],
                   '--verifier-host-outcome-digest', load('verifier.json')['digest']]
        checks = call('checks.json', 'learning-run-operational-checks', *root, *digests)
        qa = call('qa.json', 'learning-record-operational-qa', *root, *digests, '--check-receipt',
                  checks['check_receipt'], '--evaluator-assignment', str(work / 'assignment.json'),
                  '--evaluator-result', str(work / 'result.json'), '--verifier-assignment',
                  str(work / 'vassignment.json'), '--verifier-result', str(work / 'vresult.json'))
        call('close.json', 'learning-close-orchestration', *root, '--orchestration-handle',
             load('orchestration.json')['orchestration_handle'], '--status', 'completed')
        return {'checks_passed': checks.get('checks_passed'), 'accepted': qa.get('accepted')}
    raise ReviewError('review_invalid_step')
