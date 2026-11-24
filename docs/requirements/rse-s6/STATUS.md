# rse-s6 status (E1-8..E1-10, login entry status and upgrade)

- 1da80aa4 requirements + tests; 92c3a66d E1-8/E1-9 code; review 040-rse-s6 r1 (Codex) NOT-READY: 3 HIGH, 2 MED.
- Fix round (the one allowed): tests first (red run recorded), then code; green on tests.test_login_start.
  - HIGH 1: refresh calls effects.active_runtime_digest (the clone's require_authorized_collector_runtime on
    CollectorPaths) itself; the wizard passes the same to offer.
  - HIGH 2: entry read/write through checked folder descriptors (no-follow, nonblocking, uid and others-write
    checks); answer and record read only from owner-only plain files of this user, no-follow, nonblocking.
  - HIGH 3: an update never calls offer (no answers-file install); it only refreshes.
  - MED 4: fixed STALE_* reasons; a failed record write puts the earlier entry back and is not "refreshed".
  - MED 5: E1-10 runs the rendered launchd command as a real process against a temporary installed copy.
- Changed assertions: docs/requirements/rse-s6/assertions.md.
- Open for the primary: the CLI prints no reason_code for a learning-serve refusal (cli.py is a runtime file, so
  adding it moves the runtime digest).
