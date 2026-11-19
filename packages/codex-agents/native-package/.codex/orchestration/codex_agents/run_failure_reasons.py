"""S1-R6: the fixed reasons a run can fail with before its host open, for the orchestration report.

Only these are stored; the driver reports any other reason as `run_failed`.
"""
from __future__ import annotations

from .account_claim_contracts import CLAIM_DENIAL_CODES
from .preparation_reasons import PREPARATION_STAGE_REASONS

FAILED_BEFORE_OPEN_REASONS = frozenset({
    'run_failed', 'run_unavailable', 'result_capacity_full', 'run_account_call_failed', 'run_client_changed',
    'service_stopped',
}) | PREPARATION_STAGE_REASONS | CLAIM_DENIAL_CODES
# Registrations kept per orchestration (the report's MAX_RUNS).
MAX_REGISTERED_RUNS = 512
