# lostsettle status
Branch fix/lost-run-settling. Done: LRS-1..LRS-5 (see assertions.md), decision 231 round: LRS-6 (`_unheld_reply`: both warm-up conditions gate before any fenced/lost entry), LRS-7 (`run_slots.warm_up` in the capacity report, `project_service.warm_up` and summary in learning-status; docs).
LRS-4 is resolved (decision 228): test_lost_run_settlement waits past run_settling in `run_status`.
Rerun: tests.test_project_driver, test_lost_run_settlement, test_host_slots, test_refusal_steps, reply-shape and history modules (see last commit message).
