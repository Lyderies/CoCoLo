"""S1-H1d: one review's worker, verifier and operational QA as a COCOLO command (PLAN H1d-1 to H1d-5).

The same sequence the primary would otherwise run by hand, each step through this package's own
CLI, each reply saved in the work folder. Worker and verifier must give a verdict (H1d-2).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import re
import subprocess
import sys

from .collector_paths import CollectorPaths, StateError

_DIGEST = re.compile(r'[0-9a-f]{64}')
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
              and all(type(item) is dict and set(item) == {'id', 'description'}
                      and type(item['id']) is str and item['id'] and type(item['description']) is str
                      and item['description'] for item in spec['evidence']))  # F5
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


def run(step, spec, work_dir, project_root, *, cli=None, status='completed', state_root=None):
    """Run one stage (`worker`, `verify` or `finish`); returns its summary, or raises ReviewError."""
    try:
        return _run(step, spec, work_dir, project_root, cli=cli, status=status, state_root=state_root)
    except ReviewError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        # A child reply (or a saved marker) without what the next step needs stops the review plainly.
        raise ReviewError('review_step_unreadable') from None


def _run(step, spec, work_dir, project_root, *, cli, status, state_root):
    project_root = Path(project_root).resolve()  # F3: absolute before any step
    check_spec(spec, project_root)
    cli = cli or project_cli(project_root)
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    if not work.is_dir():
        raise ReviewError('review_unsafe_work_dir')
    # The state is chosen once, here: the same folder holds the key the stage bindings are made with and is
    # the one every host command below is told to use, so they cannot differ (an installed CLI pins the
    # state to its default and accepts only that one). The key is read before any child starts.
    try:
        state = CollectorPaths(None if state_root is None else Path(state_root))
        packet = (project_root / spec['packet']).resolve()
        if any(state.root == place or place in state.root.parents for place in (work, packet)):
            raise StateError('the state is inside the work folder or the packet')
        secret = state.digest_key()
    except (StateError, OSError, ValueError):
        raise ReviewError('review_state_unavailable') from None
    binding_key = hmac.new(secret, b'review-stage-binding-v1\0' + str(project_root).encode(), hashlib.sha256).digest()
    root = ['--project-root', str(project_root), '--state-root', str(state.root)]
    grant = {'tools': ['shell'], 'filesystem': [f"{spec['packet']}:read"], 'network': False, 'mcps': [],
             'external_effects': False}

    def entry(name):
        """F1: a work-folder entry that is missing or a regular file; anything else is refused."""
        path = work / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return path, False
        if not stat.S_ISREG(info.st_mode):
            raise ReviewError('review_unsafe_work_dir', name)
        return path, True

    def save(name, data):
        path, _ = entry(name)
        temporary = work / f'.{name}.{secrets.token_hex(8)}'
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            with os.fdopen(descriptor, 'w') as handle:
                handle.write(json.dumps(data, indent=1))
            os.replace(temporary, path)  # replaces the entry itself, never a file it might point to
        finally:
            if temporary.exists():
                temporary.unlink()
        return str(path)

    def load(name):
        path, present = entry(name)
        if not present:
            raise ReviewError('review_stage_not_ready', name)  # F2: the stage before has not finished
        info = os.lstat(path)
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ReviewError('review_stage_not_runner_written', name)  # not a file save() wrote
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor) as handle:
            return json.loads(handle.read())

    def facts(prefix):
        """What a stage's marker must say, taken from the stage's own saved route, assignment and result: the
        provider its route names, the verdict and summary its result states, and one digest that binds those to
        the recorded outcome, this project and this review."""
        route, assigned, answer = (load(prefix + name) for name in ('route.json', 'assignment.json', 'result.json'))
        provider = 'claude' if route['agent_type'].startswith('claude') else 'codex'
        verdict, summary = answer.get('semantic_verdict'), answer.get('summary')

        def bound(digest):
            return hmac.new(binding_key, json.dumps(
                {'digest': digest, 'provider': provider, 'verdict': verdict, 'summary': summary,
                 'review': spec['id'], 'project': str(project_root), 'stage': prefix or 'worker',
                 'route': route, 'assignment': assigned, 'result': answer},
                sort_keys=True, separators=(',', ':')).encode(), hashlib.sha256).hexdigest()
        return provider, verdict, summary, bound

    def load_stage(name, prefix):
        """A finished stage's marker, only as the runner wrote it: its mode, its exact shape, a digest equal to
        the one the stage's own recorded reply holds, and a provider, verdict and summary equal to what the
        stage's saved route and result say, under a keyed digest (HMAC) that binds them to this review and
        project (so a changed provider or verdict, or a replaced route, assignment or result, is refused).

        Whoever can write the work folder but cannot read the host's private state cannot make a binding. A
        process running as the host's own user can read that key: it is outside this check, and the project
        host is not (see `finish`). The host's records were not used instead because no command reads them
        without running a step (open and the checks do, right after this); a lookup would be a new host call."""
        stage = load(name)
        recorded = load(prefix + 'record.json')
        provider, verdict, summary, bound = facts(prefix)
        ok = (type(stage) is dict and set(stage) == {'digest', 'provider', 'verdict', 'summary', 'binding'}
              and type(stage['digest']) is str and _DIGEST.fullmatch(stage['digest']) is not None
              and type(verdict) is str and verdict
              and stage['provider'] == provider and stage['verdict'] == verdict and stage['summary'] == summary
              and type(stage['binding']) is str and hmac.compare_digest(stage['binding'], bound(stage['digest']))
              and type(recorded) is dict and recorded.get('host_outcome_digest') == stage['digest'])
        if not ok:
            raise ReviewError('review_stage_not_runner_written', name)
        return stage

    def stage_files(prefix):
        return tuple(prefix + name + '.json' for name in CHILD_FILES)

    CHILD_FILES = ('task', 'route', 'assignment', 'validate', 'preflight', 'observe', 'open', 'dispatch', 'result',
                   'observation', 'record')

    def forget(*names):
        """F2: a stage starting again first removes its own and later stages' markers."""
        for name in names:
            path, present = entry(name)
            if present:
                path.unlink()

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
        verdict = dispatched['result'].get('semantic_verdict')
        if type(verdict) is not str or not verdict:  # H1d-2: an empty verdict is none; nothing is recorded
            raise ReviewError('review_no_verdict', 'verifier' if prefix else 'worker')
        recorded = call(prefix + 'record.json', 'learning-record-host-result', '--task', task_file,
                        '--assignment', assignment_file, '--parent-grant', grant_file, '--result', result_file,
                        '--observation', observation_file, *root)
        digest = recorded['host_outcome_digest']
        return {'digest': digest, 'provider': provider, 'verdict': verdict,
                'summary': dispatched['result'].get('summary'), 'binding': facts(prefix)[3](digest)}

    if step == 'worker':
        forget('worker.json', 'verifier.json', *stage_files(''), *stage_files('v'))  # none of an earlier run stays
        orchestration = call('orchestration.json', 'learning-open-orchestration', *root)['orchestration_handle']
        worker = child('', task(spec['id'], 'security', spec['summary']), spec['objective'], spec['context'],
                       orchestration)
        save('worker.json', worker)
        return worker
    if step == 'verify':
        forget('verifier.json', *stage_files('v'))
        orchestration, worker = load('orchestration.json')['orchestration_handle'], load_stage('worker.json', '')
        verifier = child('v', task(spec['id'] + '-verify', 'verification',
                                   'Independently check the recorded review: ' + spec['summary'],
                                   {'verification_for_family': 'security',
                                    'verification_for_provider': worker['provider'],
                                    'verification_for_outcome': worker['digest']}),
                         spec['verify_objective'], [worker['digest']], orchestration)
        save('verifier.json', verifier)
        return verifier
    if step == 'finish':
        # Both stages are checked against the files they were made from (so the assignment and result paths
        # QA receives are the ones the markers bind), and the project host checks the same again: it looks
        # both digests up in its own records and compares each assignment and result body with the original
        # it holds before it commits anything.
        digests = ['--host-outcome-digest', load_stage('worker.json', '')['digest'],
                   '--verifier-host-outcome-digest', load_stage('verifier.json', 'v')['digest']]
        checks = call('checks.json', 'learning-run-operational-checks', *root, *digests)
        qa = call('qa.json', 'learning-record-operational-qa', *root, *digests, '--check-receipt',
                  checks['check_receipt'], '--evaluator-assignment', str(work / 'assignment.json'),
                  '--evaluator-result', str(work / 'result.json'), '--verifier-assignment',
                  str(work / 'vassignment.json'), '--verifier-result', str(work / 'vresult.json'))
        call('close.json', 'learning-close-orchestration', *root, '--orchestration-handle',
             load('orchestration.json')['orchestration_handle'], '--status', status)  # F4
        return {'checks_passed': checks.get('checks_passed'), 'accepted': qa.get('accepted')}
    raise ReviewError('review_invalid_step')
