# Manual setup

`./setup.sh` runs these steps for you and is the supported path. Use this page to understand what it does,
or to repair one step by hand. Every file these steps write is listed in [configuration](configuration.md).

Run every command from the root of this repository, with a trusted `python3` first on `PATH`: a system,
Homebrew or python.org Python 3.11 or later, not a virtual environment's and not macOS's
`/usr/bin/python3` shim. Every Python command uses the isolation flags `-I -B -S -X pycache_prefix=/dev/null`,
so nothing from the working folder, the environment, site-packages or a bytecode cache loads. Owner
commands (registration, status, attestation and the Codex rule commands) run this repository's own
orchestrator CLI with `--project-root`, never the project's installed copy, so keep this repository
outside every folder you open in Codex.

In the commands below, `/path/to/project` is the top folder of the project's Git working tree, and

```sh
CLI=packages/codex-agents/native-package/.codex/orchestration/cli.py
```

## Operational checks

A project that has never been set up needs `operational-checks.json`. It names the exact test and security
commands, with their hashes, that operational QA may run. Write two small Python wrappers and commit them
before computing their hashes: the installer checks that each file matches a clean commit.

Both wrappers must run **in-process**. QA denies starting another process, so `subprocess`, `os.system` and
wrappers that launch `python`, `pytest` or `gitleaks` as a command fail. Use the test runner's
Python API and a security scanner that also runs in-process. The example below uses the identifier scanner
this repository ships (`scripts/check_identifiers.py` and `scripts/qa-security.template`, copied into the
project's `scripts/` folder first); a project may add its own in-process scanner as a further security check.
A scanner needing Python packages also needs a pinned `runtime_tree` in the check configuration.

The first line is only `#!` plus the absolute trusted interpreter path, **with no interpreter flags**.
QA checks that line, snapshots the pinned interpreter and starts it directly with a minimal environment.
It does not take flags from the first line. Set `QA_PYTHON` to the real
trusted interpreter file (not a symlink or macOS's `/usr/bin/python3` shim).

```sh
cd /path/to/project
QA_PYTHON=/absolute/path/to/trusted/python3
printf '#!%s\nimport os, sys, unittest\nsys.path.insert(0, os.getcwd())\nsuite = unittest.defaultTestLoader.discover("tests")\nraise SystemExit(0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1)\n' "$QA_PYTHON" > qa-check
SCANNER=$(shasum -a 256 scripts/check_identifiers.py | cut -d' ' -f1)
sed -e "s|@QA_PYTHON@|$QA_PYTHON|" -e "s|@SCANNER_SHA256@|$SCANNER|" scripts/qa-security.template > qa-security   # the shipped identifier scan, see below
chmod 700 qa-check qa-security
git add qa-check qa-security scripts/check_identifiers.py && git commit -m "Add QA check wrappers"
INTERP=$(shasum -a 256 "$QA_PYTHON" | cut -d' ' -f1)   # sha256sum on Linux
check() { # check ID WRAPPER
  printf '{"check_id": "%s", "driver": "python-wrapper", "argv": ["./%s"], "executable_sha256": "%s", "interpreter_sha256": "%s", "runtime_tree": null, "timeout_seconds": 600}' \
    "$1" "$2" "$(shasum -a 256 "$2" | cut -d' ' -f1)" "$INTERP"
}
printf '{"version": 1, "deterministic_checks": [%s], "security_checks": [%s]}\n' \
  "$(check tests qa-check)" "$(check security qa-security)" > ../operational-checks.json
```

Give `../operational-checks.json` to the wizard when it asks, or to the installer with
`--operational-checks`. The format is described by
`packages/codex-agents/native-package/.codex/orchestration/schemas/operational-checks.schema.json` and
used by `tests/test_install.py`. Operational QA runs these commands inside macOS's sandbox (with the
`xcrun` lookup cache off) and fails closed on other systems. The sandbox also limits temporary storage
and file sizes; tests requiring larger fixtures cannot pass that QA gate unchanged.

Identifier scanning: `scripts/qa-security.template` is the shipped `qa-security` wrapper. It calls
`check_identifiers.scan_tree(root, load_required_denylist(), manifest=manifest_from_environment())`, the same function the
command line and CI use. All three scan one git-produced file list (`git ls-files -z --cached --others
--exclude-per-directory=.gitignore`: tracked files even when an ignore rule names them, plus untracked files no
`.gitignore` ignores). QA cannot start git, so the QA runner lists the files with git before the sandbox starts
and hands the list and a copy of the owner's private denylist (`$AO_IDENTIFIER_DENYLIST` or
`~/.config/codex-claude-agent-orchestration/identifier-denylist.txt`) to the wrapper in `AO_QA_FILE_MANIFEST` and
`AO_IDENTIFIER_DENYLIST` (an empty file when the owner has no denylist). The runner does not start a security
check at all (`identifier_inputs_unavailable`) when it cannot produce both, and the scan refuses (exit 2,
`REFUSED: ...`) rather than scan fewer files or with fewer terms when either is missing, unreadable or too large,
when a listed file cannot be read, when a listed path lies below a symbolic link or is a pipe, socket or device,
or when the repository's configuration moves its work tree. The wrapper carries the sha256 of
`scripts/check_identifiers.py`, filled in when you save it, and runs only that scanner: changing the scanner
means saving and approving a new wrapper. There are no project-chosen exclusions: QA must scan what CI scans
(CI also checks commit identities, which QA does not). Git lists files with ignore rules matched case-sensitively,
whatever the repository's `core.ignorecase` says. A symbolic link is scanned by name and target text, never
followed; a name that is not valid UTF-8 is a finding. Only exact lower-case placeholder accounts that are a whole
path component (`/Users/x`, `/home/user`) are allowed, and a character that can sit inside an account name
(`'`, a backquote, a bracket, `,`, `;`) ends it only as prose punctuation, never before more name; any other
name, `User` or `User Smith` included, fails. The command line refuses to scan from a subdirectory.

When a security check fails, the runner records `{path, line, category}` for at most 50 findings. It accepts a
path only if it is in the git list, uses a plain alphabet, names no host and holds no denylisted term, else it
is `[redacted]`. The line is always 0, because the runner cannot tell a real line from a number the wrapper chose.
Scanner output itself prints `[redacted]` for a file name that holds a value, is not valid UTF-8 or holds a
control or format character, and no error message names a file. Matched text is never printed.

QA discards stdout. It keeps stderr in a private folder, only to extract those `path:line: category` records,
and deletes it afterwards; the agent receives fixed diagnostic codes, the check's status, its exit code and those
records, alongside the unchanged receipt. Do not paste raw test or scanner output into run records.
For a nonzero exit, inspect the wrapper locally in your own terminal; check that it runs in-process and
that its imports and fixtures fit the sandbox.

Keep bytecode out of installed orchestration folders. Use `-I -B -S -X pycache_prefix=/dev/null` for CLI
commands: `-B` alone stops writes but still reads existing `.pyc` files. Ordinary tests or imports may leave
`__pycache__` behind; an extra file in `.codex/orchestration` makes verification and the protected command
rule refuse the install. Have the owner clean or reinstall it through setup, then verify again.

## 1. Install

Without `--apply` the installer only previews. The first install also needs the project's agent mapping
and the operational checks; the wizard passes `{"version": 1, "roles": {}, "verifiers": {}}` as the
mapping when the project has none.

```sh
python3 -I -B -S -X pycache_prefix=/dev/null scripts/install.py install --target /path/to/project --apply \
  --project-agent-routing /path/to/project-agent-routing.json --operational-checks /path/to/operational-checks.json
python3 -I -B -S -X pycache_prefix=/dev/null scripts/install.py verify --target /path/to/project
```

Commit the installed files in the project, including `.claude/settings.json`, which now holds Claude Code
deny rules for the owner-only commands (an earlier version's `.claude/agent-orchestration-permissions.json`
is migrated to your orchestrator records and deleted). If you commit by hand, skip the project's hooks:
`git -c core.hooksPath=/dev/null -c core.fsmonitor=false commit ...`. The wizard itself commits with Git
plumbing only, and stops when the project's settings name a program Git would start.

The installer also writes `.codex/hooks.json` (the learning hooks, rendered with absolute paths to this
checkout and to the Python that ran the installer) and `.codex/orchestration/local-install.json`. Never
commit them: the installed `.codex/.gitignore` ignores both, and each clone gets its own when you install
there. An update from 0.8.x replaces the old committed hooks file; take it out of Git with
`git rm --cached .codex/hooks.json` before you commit. Codex trusts hooks by their hash, so after every
install or update, once `install.py verify` passes, open `/hooks` in Codex and trust the orchestrator's
learning hooks. Only a main checkout needs this: Codex loads a linked worktree's hooks from its main
checkout. Close every Codex session in the project before installing or updating.

## 2. Enable providers

Commit `.codex/orchestration/provider-routing.json` in the project. It says which providers and Claude
models children may use. Without it, only Codex children are allowed.

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

`codex_primary_may_bridge_to_claude` lets a Codex session start Claude children. It needs Claude children
enabled and the orchestrator profile (step 5).

## 3. Register the learning collector

Once per project. Run `install.py verify` first: the wizard registers only after checking that the
project's installed orchestrator is this release, byte for byte, with nothing extra in
`.codex/orchestration`.

```sh
python3 -I -B -S -X pycache_prefix=/dev/null "$CLI" learning-register --project-root /path/to/project
python3 -I -B -S -X pycache_prefix=/dev/null "$CLI" learning-status --project-root /path/to/project
```

When the records were written by an earlier version (release 0.2.x), `learning-register` refuses with
`ledger_upgrade_required` and changes nothing. Run it again with `--upgrade-ledger` while no child is
running in any project: it keeps a checked copy of the ledger (`ledger_copy` in its reply), upgrades
the records and registers. Every other project on the machine then needs this release too.

One collector is shared by every project on the machine. Your orchestrator records (the learning state)
live in `~/.local/state/codex-agents`, or `$XDG_STATE_HOME/codex-agents` when `XDG_STATE_HOME` is inside a
hidden folder of your home such as `~/.local/state` (any other value is refused); on macOS, the first
`learning-register` after this update moves them there from `~/Library/Application Support/codex-agents`
and reports the old copy's path (`codex-agents.moved-<time>` beside the old folder) as `"state_moved"`
(null otherwise); delete that old copy once you are happy. Because a session opened on `~/Library` could
have created that old folder, it moves only with `--move-legacy-state <sha256 of its owner-allowed-signers
file, or none>` after you have checked that the owner key it names is yours; the wizard shows its creation
date and key fingerprints and asks first.

## 4. Attest billing

A human step that needs an interactive terminal:

```sh
python3 -I -B -S -X pycache_prefix=/dev/null scripts/attest.py --target /path/to/project
```

No child runs until you confirm that each account cannot be charged beyond its subscription.

- It creates the owner key at `~/.ssh/codex-agents-owner` if you have none. Any owner key must be a file in
  your own `~/.ssh` (no symbolic link on its path, the folder writable only by you), checked again before
  every use; a key anywhere else is refused. Keys without a passphrase are refused, and signing runs
  without `ssh-agent`, so no program, including an AI agent, can use your key to sign in your name. The owner key record
  must hold exactly one key.
- It asks whether pay-as-you-go extra usage and Codex credit spending are turned off, and has you sign each
  answer. It signs in memory, only the bytes it rebuilt from your answers, and checks the signature.
- A wrong passphrase offers Try again or Exit. To change the passphrase, choose "Change the owner key
  passphrase" in `./setup.sh`, or run
  `python3 -I -B -S -X pycache_prefix=/dev/null scripts/attest.py --change-passphrase`.

Run it again whenever your billing settings change. If you lose the passphrase, choose "Replace a lost owner
key" in `./setup.sh`.

## 5. Optional: Codex orchestrator profile

Codex runs commands without network by default, so a Codex primary cannot reach Claude children. Create
`~/.codex/orchestrator.config.toml` (mode 0600):

```toml
sandbox_mode = "workspace-write"

[sandbox_workspace_write]
network_access = true
```

Start orchestrating sessions with `codex -p orchestrator`. Ordinary sessions keep network off, and your
main `config.toml` is never edited.

## 6. Codex commands rule

Codex's sandbox blocks the learning collector, so a Codex primary needs a Codex command rule that lets only
this project's 27 everyday orchestrator commands run outside the sandbox. Use the `python3` your Codex
sessions find first on `PATH`:

```sh
python3 -I -B -S -X pycache_prefix=/dev/null "$CLI" install-codex-rule --project-root /path/to/project
```

It copies exactly this package's runtime files, verified byte for byte, to an owner-only
`~/.codex/agent-orchestration/<project digest>/<runtime digest>/` folder bound to the project, then writes
`~/.codex/rules/agent-orchestration-<digest>.rules` naming that copy, the resolved base interpreter and the
isolation flags, and prints both. `--print` shows the rule without writing it, and `--replace` replaces a
differing one. It refuses:

- an interpreter that is a virtual environment's, the macOS shim, inside the project or a temporary
  folder, writable by another user, or outside the trusted install folders (system folders, Homebrew,
  python.org, Xcode and the Command Line Tools, `/Applications`, Nix, or a hidden top-level folder of
  your home folder such as `~/.pyenv` or `~/.local`);
- a project in a temporary folder, a project without an install manifest, and one whose
  `.codex/orchestration` holds anything the package does not install (a module, package, bytecode or link);
- a Codex home other than your account's real `~/.codex`: a `HOME` or `CODEX_HOME` that disagrees with the
  system account database, or a symlinked `~/.codex`.

Run it again after a Python upgrade or a package update; `host-command` then reports `stale`. Before each
command, the copy's session guard refuses a Codex session that was opened on a folder that contains the
project, that can write `/`, your home folder or a folder above it, `~/.codex`, the copy, this repository,
your records or a tool folder, or that runs without its sandbox or leaves no record. Open Codex on the
project itself: never on a folder that contains a set-up project, this repository, your home folder, a
folder above it or `/` (Codex runs a nested trusted project's `.codex/config.toml` and hooks outside the
sandbox), and never add `~`, `~/.codex` or `~/.local` with `--add-dir`.

To undo only the rule and its copy:

```sh
python3 -I -B -S -X pycache_prefix=/dev/null "$CLI" remove-codex-rule --project-root /path/to/project
```

It prints `{"ready": true, "removed": [...]}`, with an empty list when there was nothing to remove.

## Remove everything

```sh
python3 -I -B -S -X pycache_prefix=/dev/null scripts/install.py remove --target /path/to/project --apply
```

It first removes the Codex commands rule and its protected copy with this repository's own
`remove-codex-rule`, whatever version the project has installed, then the permission entries it added,
then both packages.
