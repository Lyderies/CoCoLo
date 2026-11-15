"""Bounded parsed inputs for original-open admission, never signature authority.

Only the account owner's actual returned-worker registry may supply this data.
Final raw recapture, current permission and atomic original-open commit remain
mandatory. No caller, request JSON or dataclass may supply a passed verdict.
"""
from .admission_metadata import FIVE_HOURS_MS, evaluate_admission
from .claim_admission import PreparedClaimAdmission
from .account_open_contracts import OpenError


def open_admission_inputs(prepared, verified_ids):
    if type(prepared) is not PreparedClaimAdmission or type(verified_ids) is not frozenset:
        raise OpenError('open_preparation_failed')
    snapshot = prepared.snapshot
    provider, project = snapshot.provider, snapshot.project_id
    local = sorted((item for item in prepared.rows if item.raw[1] == project), key=lambda item: item.raw[3])
    account = sorted(prepared.rows, key=lambda item: (item.raw[8], item.raw[9], item.raw[1], item.raw[3]))
    identities = tuple((item.record, item.raw[6]) for item in local
        if item.raw[4] == 'provider_identity' and item.raw[7] == 'provider' and item.record['provider'] == provider)
    attestations = tuple((item.record, item.raw[6]) for item in local
        if item.raw[4] == 'billing_attestation' and item.raw[7] == 'owner'
        and item.raw[2] in verified_ids and item.record['provider'] == provider)
    observations = {kind: tuple((item.record, item.raw[6]) for item in account
        if item.raw[4] == kind and item.raw[7] == 'provider' and item.record['provider'] == provider)
        for kind in ('provider_billing', 'provider_headroom')}
    admissions = tuple(item.record for item in prepared.rows
        if item.raw[4] in {'admission_decision', 'probe_admission'} and item.raw[7] == 'lifecycle')
    cited = frozenset(item.raw[6] for item in local
        if item.raw[4] in {'provider_identity', 'billing_attestation', 'provider_billing', 'provider_headroom'}
        and item.record['provider'] == provider)
    return {'provider': provider, 'identities': identities, 'attestations': attestations,
        'billing': observations['provider_billing'], 'headroom': observations['provider_headroom'],
        'prior_claude_admissions': frozenset(record['account_fingerprint'] for record in admissions
            if record['provider'] == 'claude' and record['decision'] == 'pass' and record['account_fingerprint'] is not None),
        'prior_admissions': tuple((record['account_fingerprint'], record['observed_at_ms']) for record in admissions
            if record['record_type'] == 'admission_decision' and record['provider'] == provider
            and record['decision'] == 'pass' and record['run_id'] is not None and record['account_fingerprint'] is not None),
        'cited_digests': cited, 'admission_policy': {'client_versions': {'minimum': snapshot.policy.minimum_version,
            'maximum_exclusive': snapshot.policy.maximum_exclusive_version},
            'max_admissions_per_window': snapshot.policy.max_admissions_per_window}}


def evaluate_original_open(inputs, *, timestamp, guard_environment):
    # All dictionaries here came from already validated, actually returned raw
    # inputs. No JSON or signatures are parsed on the final account transaction.
    return evaluate_admission(provider=inputs['provider'], identities=inputs['identities'], attestations=inputs['attestations'],
        billing=inputs['billing'], headroom=inputs['headroom'], window_headroom=inputs['headroom'],
        prior_claude_admissions=inputs['prior_claude_admissions'], environ=guard_environment, now_ms=timestamp,
        admission_policy=inputs['admission_policy'], cited_digests=inputs['cited_digests'],
        prior_run_admissions=[(fingerprint, when) for fingerprint, when in inputs['prior_admissions']
                              if when >= timestamp-FIVE_HOURS_MS])
