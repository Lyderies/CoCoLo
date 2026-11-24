# TF-27c LOWs (listed, not fixed; merge bar: LOW listed)
From the Claude cross-provider check (2026-11-24) of 475958a0..52089d64:
- LOW-1: binding_state accepts a binding with an empty `manifests` list (no authorization runs; rebinder refuses to create one). Fix: reject `not data['manifests']`.
- LOW-2: prior_migrated filter does not recheck lists at the current introduction; a re-listed migrated ID is caught later by the runner's shrink-only check. Fix: keep IDs in the seed when listed at the candidate revision.
- LOW-3: build() runs check_later_retirements on normal migrations at introduction for a sole binding too (stricter than 475958a0). Recorded as intended in TF-27c-2.
- LOW-4: wrong binding authority is witnessed only at the rebinder, not at the gate. Fix: add a gate-level refusal case.
From the Codex r2c check: MED-6 evidence kills the mutant during fixture setup, not at the final assertion.
