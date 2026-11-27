# Configuration

The [setup wizard](setup-wizard.md) writes most of this for you. This page lists every file and option it
touches, for reference and for repairs. The file and folder names predate the name COCOLO and are kept as
they are. The commands that write them are in [manual setup](manual-setup.md).

## Project files

| File (in the project) | Purpose |
|---|---|
| `.codex/orchestration/provider-routing.json` | Enabled providers, allowed Claude models, and `codex_primary_may_bridge_to_claude`. Committed and clean; without it only Codex children are allowed |
| `.codex/orchestration/operational-checks.json` | The exact, hashed test and security commands operational QA may run |
| `.codex/orchestration/project-agent-routing.json` | Optional mapping of project agents to task families; setup writes an empty one if missing |
| [`docs/routing-table.md`](routing-table.md) (this repository) | The starting table; the policy mirrors it, and a test holds the policy to its order |

### `provider-routing.json`

Says which providers and Claude models children may use. Commit it in the project; setup's step 3
writes it.

```json
{
  "version": 1,
  "providers": {
    "codex": {"enabled": true},
    "claude": {"enabled": true, "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]}
  },
  "codex_primary_may_bridge_to_claude": false
}
```

- `providers.<name>.enabled` turns a provider's children on or off. To stop a provider's children,
  disable it here.
- `providers.claude.models` lists the Claude models children may use. Models you have not enabled stay
  disabled.
- `codex_primary_may_bridge_to_claude` (default `false`) lets a Codex session start Claude children. It
  needs Claude children enabled and the orchestrator profile below.

### `operational-checks.json`

Names the exact commands operational QA may run, each with the SHA-256 of its executable and
interpreter. A project that has never been set up needs one before the first install; the
[recipe](manual-setup.md#operational-checks) builds it from two small, committed wrapper scripts.

- The top level has `version` (1), `deterministic_checks` (your tests) and `security_checks` (for example
  a secret scanner).
- Each check has `check_id`, `driver` (`python-wrapper`), `argv`, `executable_sha256`,
  `interpreter_sha256`, `runtime_tree` and `timeout_seconds`.
- The schema is `packages/codex-agents/native-package/.codex/orchestration/schemas/operational-checks.schema.json`.
- Operational QA runs these commands inside macOS's sandbox and fails closed on other systems.

### `project-agent-routing.json`

Optional. Maps the project's own agents to task families. When the project has none, the wizard
installs `{"version": 1, "roles": {}, "verifiers": {}}`.

### Files the installer writes

- Commit the installed files, including `.claude/settings.json`, which holds the Claude Code deny rules
  for the owner-only commands. Your existing settings are kept.
- Never commit `.codex/hooks.json` (the learning hooks, rendered with absolute paths to this checkout and
  the trusted Python) or `.codex/orchestration/local-install.json`. The installed `.codex/.gitignore`
  ignores both, and each clone gets its own.

## Machine files

| Path | Purpose |
|---|---|
| `~/.local/state/codex-agents` | Your orchestrator records: the shared ledger, the owner key record and the collector's files. Never committed |
| `~/.local/state/codex-agents/projects/<project id>` | Each project's own history (runs, results, QA, learning), owned by that project's service |
| `~/.ssh/codex-agents-owner` | The passphrase-protected owner key that signs billing attestations; `attest.py` creates it if you have none, and a replacement creates `codex-agents-owner-2` or the next free number |
| `~/.codex/orchestrator.config.toml` | The optional Codex profile with sandbox network on, for `codex -p orchestrator` (step 7) |
| `~/.codex/rules/agent-orchestration-<digest>.rules` | The optional Codex commands rule (step 8) |
| `~/.codex/agent-orchestration/<project digest>/<runtime digest>/` | The owner-only protected copy of the orchestrator that the rule runs |

The profile (mode 0600) contains the following; ordinary sessions keep network off, and your main
`config.toml` is never edited:

```toml
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
```

## Shared user settings

The account state folder can contain an optional `user-settings.json`. Edit it to
change ledger and project-store warnings without restarting the collector or
changing the installed runtime. The default state folder is listed above;
`XDG_STATE_HOME` can move it as described below.

```json
{
  "store_size_bytes": 8589934592,
  "store_growth_bytes_per_day": 1073741824,
  "disk_free_bytes": 2147483648,
  "disk_free_percent": 5,
  "no_merge_warning_ms": 5400000,
  "memory_available_percent": 10
}
```

These defaults warn at 8 GiB, or growth over 1 GiB per day. They report to status
and its owner attention list; new runs remain allowed. Size/growth values use
integer bytes, at least 1 MiB and at most 2^63-1.
Growth compares at least six hours, avoiding alarms from short bursts after a
baseline renewal or WAL truncation. A backwards clock resets the growth baseline.
Any key can be omitted to use its default. The next health pass
reloads changes, and the health summary displays the S9 values in force, including
before the first pass. Project measurements refresh as the bounded scan visits them. An invalid file produces a
plain warning and uses the defaults. Use an ordinary file owned by your account;
symlinks and special files are reported as invalid. Duplicate/unknown keys and
non-integer values also make the whole file invalid; warnings contain no file content.

This is the single file and shared `codex_agents.user_settings.load` loader for
all user limits. S10 uses `disk_free_bytes` (default 2 GiB, minimum 1 MiB) and
`disk_free_percent` (default 5, range 1-99): a new run is refused when free space
on the workspace folder or on the account state folder is below the larger of the
two (2 GiB on disks under 40 GiB, 5% above). The refusal says which folder, how
much is free and needed, and how to change the limit; if the free space cannot be
read the run is also refused. The check happens before anything is recorded for the
run; the open command then exits with an error that gives the reason. Low disks are also
shown in the status warning list, and so is an invalid settings file (the open command's
reply also carries `settings_warning` while the defaults are in force).
The VM disk joins in S19. `memory_available_percent` (default 10, range 1-99)
only reports memory pressure in status; it never refuses a run. S35 will use
`no_merge_warning_ms` (default 5,400,000 ms = 1.5 h, minimum 60,000 ms) for the
no-merge warning. Integer limits have a maximum of 2^63-1
unless a narrower range is stated. Defaults record owner decisions 340/341.

## Environment

| Variable | Effect |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | Must not be set; admission fails closed on an API key in the environment |
| `XDG_STATE_HOME` | Moves your records to `$XDG_STATE_HOME/codex-agents`, only when it points inside a hidden folder of your home such as `~/.local/state`; any other value is refused |
| `CODEX_HOME` | Leave it unset or pointing at `~/.codex`; the Codex rule works only in your account's real `~/.codex` |
| `NO_COLOR` | `NO_COLOR=1` gives the wizard plain output |
