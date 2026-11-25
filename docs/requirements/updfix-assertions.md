# UPDFIX: assertions changed (old -> new)

One existing assertion was changed, in the lifecycle checks copied from the T7 S5 branch (commit 54bd4908, copied here only
so the L12.3 checks can run on this branch; the copy is the first commit of fix/update-refusal):

| Where | Old | New | Why |
|-------|-----|-----|-----|
| scripts/e2e_checks_update_safety.py `l12_3_update_with_a_running_child_is_refused_plainly_and_the_child_goes_on` | `phase.expect(states_of(update).get(5) == "FAIL", "the refusal is at the collector step")` | `phase.expect(not any(state == "OK" for number, state in states_of(update).items() if 3 <= number <= 8), "the refusal comes before the install step: steps 3 to 8 (providers to commands) ran none of their work")` | UPDFIX-3: the refusal now comes in step 2, before the install, instead of at the collector step after the install had written and committed files (the defect). Every other assertion of the three L12.3 checks is unchanged, including the red-by-design ones, which are now expected green. |

No test assertion of tests/ or packages/ was removed or edited; tests were added (packages/codex-agents/tests/test_updfix.py,
tests/test_setup_update.py `test_UPDFIX_*`, fake `FAKE_CHILDREN` / `FAKE_REGISTER_BUSY` replies in tests/wizard_direct_driver.py).
