# UPDFIX: assertions changed (old -> new)

One existing assertion was changed, in the lifecycle checks copied from the T7 S5 branch (commit 54bd4908, copied here only
so the L12.3 checks can run on this branch; the copy is the first commit of fix/update-refusal):

| Where | Old | New | Why |
|-------|-----|-----|-----|
| scripts/e2e_checks_update_safety.py `l12_3_update_with_a_running_child_is_refused_plainly_and_the_child_goes_on` | `phase.expect(states_of(update).get(5) == "FAIL", "the refusal is at the collector step")` | `phase.expect(not any(state == "OK" for number, state in states_of(update).items() if 3 <= number <= 8), "the refusal comes before the install step: steps 3 to 8 (providers to commands) ran none of their work")` | UPDFIX-3: the refusal now comes in step 2, before the install, instead of at the collector step after the install had written and committed files (the defect). Every other assertion of the three L12.3 checks is unchanged, including the red-by-design ones, which are now expected green. |

No test assertion of tests/ or packages/ was removed or edited; tests were added (packages/codex-agents/tests/test_updfix.py,
tests/test_setup_update.py `test_UPDFIX_*`, fake `FAKE_CHILDREN` / `FAKE_REGISTER_BUSY` replies in tests/wizard_direct_driver.py).

## Fix round 1 (review 040-updfix-r1)

| Where | Old | New | Why |
|-------|-----|-----|-----|
| tests/test_setup_update.py `test_UPDFIX_3_a_check_that_does_not_refuse_leaves_the_update_as_it_was` (added in this unit) | the replies list included `"not json at all"` and expected the update to go ahead | that reply removed from the list; it is now one of the replies of `test_UPDFIX_3_a_check_that_cannot_tell_refuses_before_anything_is_installed`, which expects exit 1, the plain "could not tell" sentence and nothing changed | Decision 265: the pre-check fails closed (UPDFIX-2/3 text reconciled in updfix.md). |
| packages/codex-agents/tests/test_updfix.py `test_UPDFIX_2_no_state_at_all_is_not_a_refusal` | unchanged | unchanged (a state folder with no ledger still goes ahead; `test_UPDFIX_2_a_state_folder_with_no_ledger_is_not_a_refusal` added) | Only no ledger at all proceeds. |
| scripts/e2e_checks_update_safety.py `l12_3_update_with_a_running_child_is_refused_plainly_and_the_child_goes_on` | (round 0 replacement) "no step 3..8 is OK" alone | that assertion kept, plus: the text contains "The update stopped (child_run_active)" and step 2 is not OK | The first alone passes an empty summary. |
| UPDFIX-2 text in updfix.md | "When it cannot tell (no ledger, no collector, an unreadable or older ledger) it says refused: false" | only no ledger at all says refused false; an unreadable ledger refuses | Decision 265. |

## Fix round 2 (review 040-updfix-r2c)

No existing assertion was changed or removed. Added only: the older-schema, wrong-shape, unreadable-folder, brief-lock and
persistent-lock tests in packages/codex-agents/tests/test_updfix.py, and
`test_UPDFIX_3_a_refusal_at_the_check_before_the_install_leaves_the_remembered_projects_alone` in tests/test_setup_update.py.
The requirement text of UPDFIX-2 and UPDFIX-3 in updfix.md is reconciled (older ledger proceeds, lstat, remember after the
pre-apply check, busy retry).
