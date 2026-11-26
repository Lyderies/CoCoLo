# S44a: changed or removed assertions

No existing assertion was changed or removed.

One draft of a new test was corrected before it first went green (it is new in this step): the window-with-no-data test
had expected `no_runs_in_window` for a window that ends before the first recorded run, which the requirement calls
`before_usage_recorded`; it now uses a window between two recorded runs and a separate test covers the earlier window.
