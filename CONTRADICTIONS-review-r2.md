# Round-2 fixture/requirement reconciliation

`BudgetDebtTests.test_baseline_debt_warns_even_unselected_and_only_regression_fails` proves TF-8 debt retirement after a complete module. Its helper previously supplied no expected-fragment plan, relying on the reviewed M3 defect to infer completeness from returned rows. That incomplete fixture cannot establish the requirement. The helper now supplies its independently declared one-fragment schedule (`root:old -> old`); all budget, retirement and partial-shard assertions are retained. No expected result was changed.
