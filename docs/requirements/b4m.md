# B4m: LR-1 compatibility messages and reply fields

Scope: section 3 of ANALYSIS-0.4.0-rsb-b4-b7.md, RS-B-B4m-1..3. Build option A as requested;
owner decision 129.3 remains pending. No VERSION or runtime-releases.json change.

**RS-B-B4m-1 — One local update command.** One function builds the exact terminal command
from the local install record: change into the installing clone, then run `./setup.sh <project>`
and choose Configure. All three compatibility refusal sites use it, name the version holding
the state, and use plain words. Collector replies never supply command paths or executable text.

**RS-B-B4m-2 — Preserve existing reply fields.** A newer collector/account never adds or removes
fields of an existing account/history operation's reply. New data requires a new operation.
The guard compares every existing operation with the previous-release field catalogue, retaining
the optional field variants already allowed by that release.

**RS-B-B4m-3 — Help already-installed clients.** Troubleshooting and release notes name
`status_collector_unavailable` and `runtime_newer`, show the clone/project terminal command,
and direct already-installed 0.3.x users to Configure.

## Requirement to test map

All names below are in `packages/codex-agents/tests/`.

| Requirement | Tests | Implementation |
| --- | --- | --- |
| RS-B-B4m-1 | `test_b4m_compatibility.CompatibilityMessageTests.test_old_client_newer_collector_names_command` | `compatibility_message.py`: real locally read binding; shell quotes clone/project; test executes the command in `/bin/sh`, with quote/space/metacharacter paths and poisoned collector path fields |
| RS-B-B4m-1 | `CompatibilityMessageTests.test_three_sites_share_the_command` | `collector_client.health`, `learning_store.authorize_collector_runtime`, CLI `ProjectServiceOtherVersion` handler all call the same builder; version and command assertions on all three |
| RS-B-B4m-1 | `CompatibilityMessageTests.test_registration_from_clone_keeps_the_project_in_the_command` | Registration passes the project root to authorization, including when registration runs from the clone |
| RS-B-B4m-1 | `CompatibilityMessageTests.test_missing_local_record_does_not_guess_a_clone` | Missing/invalid binding produces plain recovery guidance without inventing an exact clone path |
| RS-B-B4m-1 | `CompatibilityMessageTests.test_project_service_version_comes_from_local_hint_and_release_record` | Service digest from its local hint maps to the installing clone's release catalogue; unavailable versions are described as unavailable |
| RS-B-B4m-2 | `test_reply_shape_compatibility.ReplyShapeCompatibilityTests.test_every_existing_reply_matches_previous_release_catalogue` | All 46 account/history operations plus response envelopes; exact required/optional sets, no removed operation; new operations require their own entry |
| RS-B-B4m-2 | `ReplyShapeCompatibilityTests.test_added_and_removed_fields_are_refused_for_every_operation` | Every optional variant, added field, and removed required field exercised against the production gate |
| RS-B-B4m-2 | `ReplyShapeCompatibilityTests.test_both_apis_apply_the_shape_guard_before_value_checks` | Both real API validator entry points must call the gate for every operation; existing value/nested contract checks follow it |
| RS-B-B4m-3 | `test_b4m_compatibility.CompatibilityDocsTests.test_old_installs_have_the_codes_and_terminal_command_in_docs` | `docs/troubleshooting.md`, `CHANGELOG.md` |

## Catalogue provenance and boundaries

`tests/fixtures/reply-fields-8d4a7ce3.json` was captured from the supplied pre-B4m base
8d4a7ce3 (package 0.8.55), before production edits. There was no account/history reply-field
catalogue in that tree; this fixture establishes it. It records operation result fields and the
common response envelope. Existing optional fields (`billing_waivers`, billing probe metadata,
and `run_slots`) retain their previous accepted variants. The orchestration report's field list
comes from its existing producer, whose old validator did not close the outer shape. Nested
records retain their existing validators; this catalogue does not replace their value/scope checks.

`reply_shapes.py` is the runtime table; both service implementations already validate outgoing
success replies through their API decoder, so the same gate protects producers and consumers.
Refusal envelopes and wire versions are unchanged. Both new modules are in `RUNTIME_FILES`.

## Red evidence and validation

Before any production edit, the two new modules ran with `python3 -B -m unittest
tests.test_b4m_compatibility tests.test_reply_shape_compatibility`: 8 tests, 0.018 seconds,
6 failing subtests and 51 errors (absent new modules/gate). A second captured run is in
[b4m/red.json](b4m/red.json). The clone-registration test was added before its implementation:
1 test, 0.003 seconds, red on the missing `project_root` keyword. No existing assertion changed;
the new local-binding fixture's manifest was corrected to list its CLI.

Validation results and socket-blocked checks are recorded in [b4m/validation.md](b4m/validation.md).
The current-runtime digest release pin is deliberately left for the primary's release bookkeeping;
the exact runtime file-set check is run independently.
