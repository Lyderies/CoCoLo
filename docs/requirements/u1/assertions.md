# U1 assertion changes

Original build: no pre-existing assertion removed or changed.

## Fix round

- `test_events_replay_and_reserved_close_roll_repeatedly_past_limit`: old
  `('completed', 3)` -> new `('rolled_over', 3)` for predecessor status/sequence.
  The reserved-close count stays exact; rollover continues unfinished goals.
- `test_handoff_links_packet_and_state_survive_restart_without_file`: old report
  status `completed` -> new `rolled_over`, for the same reason. Existing packet,
  replay and single-open assertions remain. Added successor status `paused`.
- New session/capacity pause, cumulative accounting, complete rollover report,
  flat SQLite work, and additive-index reopen tests strengthen O0C-U1-2/3/4/5.
  No assertion is removed or weakened.
