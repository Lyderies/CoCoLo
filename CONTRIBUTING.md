# Contributing

Thanks for your interest. Please open an issue before sending a pull request, so we can agree on the
change first. Report security problems privately as described in [SECURITY.md](SECURITY.md), never in a
public issue.

## Branches

- `main` holds releases only.
- `version/<semver>` is where development for the next release happens. Each one is cut from `main`.
- `feature/<name>` branches come off the current version branch and merge back into it once their checks
  and any required review pass.
- The owner merges a version branch into `main` after the full regression run and CI pass.
- `main` is protected by a ruleset: changes only through pull requests, one approving review (admins
  may bypass), every review conversation resolved, the branch up to date with `main`, and these checks
  passing: `checks`, `unit (ubuntu-latest)`, `unit (macos-latest)` and `integration (1)` to
  `integration (6)`. No force push and no deletion. Release tags (`v*`) cannot be moved or deleted.
  Changing the number of integration shards in `.github/workflows/ci.yml` means changing the
  ruleset's required checks too.
- `planning` is a separate branch, with no shared history, for plans, design working documents and
  research. See [Planning material](#planning-material).

## Checks

Turn on the repository's hooks once, then run the checks before every pull request:

```sh
git config core.hooksPath .githooks          # identifier guard on every commit
python3 scripts/check_identifiers.py         # scan tracked files
ruff check .
python3 scripts/parallel_tests.py            # all three suites, in parallel
```

CI runs the identifier guard and Ruff, then the tests by stage: a pull request runs the unit tests on
Linux and macOS and the integration tests on Linux; `main`, tags and a nightly run add the end-to-end
tests on both systems and the integration tests on macOS. The integration tests run in six shards,
balanced by `scripts/test-timings.json`, each at half the runner's cores
(`parallel_tests.py --shard K/6`). Measured on GitHub's runners (November 2026), a pull request's
checks take about 4 minutes, its slowest shard about 2 to 5 minutes. A failing shard uploads its failed
units' logs as an artifact for 7 days.

`python3 -B scripts/parallel_tests.py` runs all three suites in parallel and prints one summary; a
failed unit's log tail is shown. Never run them one after another except to debug: that takes over half
an hour. Every test module has a stage in `scripts/test-stages.json`: `unit` (no process, socket or
sleep; a unit test that does one fails), `integration` (real services, children, sockets, git,
sandboxes) or `e2e` (the setup wizard and the installer run as programs). `--stage unit` runs only the
unit tests, in seconds. While working, run only the modules you touched, for example
`python3 scripts/parallel_tests.py --module codex:tests.test_bridge --module root:test_install`. A new
module needs a stage; `python3 scripts/measure_test_behaviour.py` measures what each module does and
proposes one.

- Every runtime change needs a Codex package version bump and an entry in
  `packages/codex-agents/runtime-releases.json`.
- Changes to authority, containment or billing logic need an independent security review before
  release, recorded in the CHANGELOG. Everything else needs tests, Ruff and a clean identifier scan.
- Describe user-visible changes in [CHANGELOG.md](CHANGELOG.md).
- Before opening a release pull request from `version/*` to `main`, try that release on a real
  install from its `version/*` branch. Record what ran and its results; unit tests and a disposable
  install alone do not satisfy this release gate.

## No identifiers

This repository is public. Never commit personal names, email addresses, hostnames, home paths or
machine pins, and keep run records and ledgers out of it (they live in `~/.local/state/codex-agents`).
Commit with a GitHub `noreply` address.

`scripts/check_identifiers.py` scans tracked files, and the pre-commit hook scans staged content and your
commit identity. Findings report only a category, path and line, never the matched text. Terms specific
to you can go in a private denylist outside the repository, one per line:
`~/.config/codex-claude-agent-orchestration/identifier-denylist.txt`, or the file `$AO_IDENTIFIER_DENYLIST`
names.

## Documentation

User and contributor documentation lives in [docs/](docs/README.md). The
[security model](docs/security-model.md) and SECURITY.md's known limits must agree; update both when a
limit changes.

## Planning material

Plans, design working documents and research live on the `planning` branch, not on `main` or the version
branches. Check it out beside your clone:

```sh
git fetch origin planning
git worktree add ../planning planning
```

That branch has no scripts of its own; run the identifier scan there from your code checkout:
`(cd ../planning && python3 "$OLDPWD/scripts/check_identifiers.py")`.

## License

Contributions are accepted under the [Apache License 2.0](LICENSE) (section 5), the project's license.
