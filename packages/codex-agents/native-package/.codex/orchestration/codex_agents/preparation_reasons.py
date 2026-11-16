"""Why a run's preparation failed, by stage: fixed identifiers, safe to report.

A refused run says what stopped it instead of only `run_unavailable`. No exception
text, path or private input is ever reported, only one of these names.
"""

PREPARATION_STAGE_REASONS = frozenset({
    'preparation_invalid_input', 'preparation_not_authorized', 'preparation_managed_configuration',
    'preparation_client_missing', 'preparation_client_unsupported', 'preparation_client_changed',
    'preparation_export_failed', 'preparation_backend_refused',
    # No free preparation slot or budget, or the lane was busy, when the run was submitted.
    'preparation_full', 'preparation_busy',
    # An unexpected error inside preparation, at no named stage.
    'preparation_unavailable',
    # Handing the prepared run to its worker was refused: the pool is full, or failed.
    'preparation_worker_busy', 'preparation_worker_failed',
    # An unexpected error at one step of building the run's inputs.
    *(f'preparation_failed_at_{step}' for step in (
        'decode', 'scope', 'assignment', 'route', 'roots', 'role_prompt', 'worker_input', 'read_paths',
        'client', 'workspace', 'worker_result', 'client_recheck', 'body', 'decode_check')),
})
