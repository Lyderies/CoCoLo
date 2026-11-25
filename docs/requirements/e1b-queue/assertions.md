# Assertion accounting

No pre-existing repository test assertion is changed or removed. New assertions
implement the E1b-3/4/7 contract in `../e1b-queue.md`; tests preceded runtime code.

## Unit fixture correction after the first registered run

TF-12 rejects positive blocking condition/Event waits in unit tests. The first
registered run had metadata_errors [] but its real thread gates were rejected
by that guard. The replacement is a synchronous frontend continuation inside
the running operation, handed off by entered/release events. All custody,
deadline, effect-order, refusal and handled assertions retain their original
conditions. This is a test-fixture correction to satisfy the requested unit
layer, not a relaxation of queue behavior or a guard bypass.

| Original assertion | Replacement | Reason |
| --- | --- | --- |
| `finish_owner`: `assertFalse(thread.is_alive())` after join | In each former thread-driven test, `assertTrue(queue.execute_next())` followed by the unchanged `assertTrue(handle.handled.wait(1))` | Synchronous pump return plus handled acknowledgement proves completion without a blocking join. |
| `finish_owner`: `assertEqual(errors, [])` | Direct `execute_next()` calls propagate unexpected exceptions to unittest | No helper thread needs an error mailbox. |
| Gate: raise `AssertionError` if `release.wait(1)` is false | `assertTrue(release.wait(0), "callback release event missing")` | The deterministic continuation must release before callback continuation, with no blocking wait. |
| Wrong-owner `assertRaisesRegex(RuntimeError, "owner")` in a second real thread | Identical assertion with an injected different `current_thread()` identity | Checks the same owner-identity refusal without thread completion waits. |
| Entered, unfinished handled/custody, deadline and partial-effects assertions in the three running-gate tests | Identical assertions inside the frontend continuation, between entered and release events | Observe precisely the same running state under deterministic interleaving. |

Real threaded contention belongs to later owner integration. This slice makes
no claim of a process/thread stress run.
