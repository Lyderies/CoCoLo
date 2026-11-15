"""Why a run's preparation failed, by stage: fixed identifiers, safe to report.

A refused run says what stopped it instead of only `run_unavailable`. No exception
text, path or private input is ever reported, only one of these names.
"""

PREPARATION_STAGE_REASONS = frozenset({
    'preparation_invalid_input', 'preparation_not_authorized', 'preparation_managed_configuration',
    'preparation_client_missing', 'preparation_client_unsupported', 'preparation_client_changed',
    'preparation_export_failed', 'preparation_backend_refused',
})
