# Assertion changes

No assertions in tests present at the unit's base are removed or changed. S14 is absent from this unit's
base; its accepted measurements are used as a sourced projection input and its
backup boundary checks remain a primary integration gate when S14 is merged.

New S14b test revision, before outcome retention code: the real-footprint test's
`assertIsNotNone(unreferenced outcome)` is replaced by `assertIsNone(unreferenced
outcome)`, an exact outcome-count/status summary assertion and a digest lookup
returning that summary. Reason: the reader audit proves a 90-day learning window;
keeping all full outcomes failed measured sizing once released receipts and
partial summaries were included. Linked outcomes still have the original
preservation assertion, with an explicit evidence link in their fixture. This
strengthens the lifecycle requirement rather than weakening a storage threshold.

## Fix round (040-s14b-r1)

- All retention fixture writes previously called `_append_checked_locked` with
  unchecked/unlinked detail -> `store_product_row` validates each row, validates
  evidence, and invokes the product append (including `_auto_link_locked`) in
  one transaction. Outcomes first store a matching terminal host event. The
  integration outcome now belongs to the actual claimed run, rather than an
  unrelated synthetic run. This replaces both reviewed fixture bypasses.
- Unreferenced outcome sweep count `1` -> `2`, and summary `{'outcome': 1}` ->
  `{'event': 1, 'outcome': 1}`: the terminal host event and its linked outcome
  expire atomically. The released-capacity budget check also changes `1` -> `2`.
- Protected fixture total `3` -> `4`: product provenance adds the terminal host
  event, which remains protected together with the controlled outcome.
- `test_historical_lookup_past_SQL_work_limit_returns_plain_age_answer` expected
  an invented generic age answer after read refusal ->
  `test_historical_lookup_budget_failure_does_not_invent_membership` requires
  SQLReadError instead. A budget failure cannot establish that an ID existed.
  Indexed, typed member and typo checks at 365,001 summaries replace the previous
  unbounded JSON scan/fallback assertion. Existing not-found assertions remain.
- The integration live-run check additionally requires the outcome to remain
  present while its real binding/open is live. No footprint threshold is raised.

New checks cover a quiet protected tail remaining idle, the owner request's
existing 800 ms budget, a protection recheck inside the write transaction,
propagated completion replay fences, and rollback/release of a linked group.

- The former lookup mock's `assert_called_once()` is retained in the new refusal
  check. Its old outcome fixture/digest path is covered by the strengthened
  product outcome/status/digest test; the new refusal check uses a never-written
  ID to prove that exhausted work cannot invent historical membership.
- The footprint assertion now includes the derived lookup index, and executes
  after backup verification. The 16 GiB ceiling is unchanged: the corrected
  projection fails at 16.464 GiB. The earlier 14.219 GiB claim is superseded.
- New closed-contract assertions reject malformed member types. Existing
  lifetime-cap registry entries now name the indexed lookup check; the bounded
  linked-component entry names the oversized-group/no-starvation check.
