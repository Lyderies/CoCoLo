# COCOLO

**Claude Orchestrator, Codex Orchestrator, Local Orchestrator**

[![CI](https://github.com/Lyderies/codex-claude-agent-orchestration/actions/workflows/ci.yml/badge.svg)](https://github.com/Lyderies/codex-claude-agent-orchestration/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

COCOLO lets the Claude Code or Codex session you work in hand small, well-defined jobs to helper
agents: a review, a piece of research, a plan. The helpers can be Claude or Codex, whichever does that
kind of job best for the least usage. They only read your project, never change it. Everything runs on
your own machine, on the subscriptions you already pay for, and never on pay-per-use billing.

**Status:** release 0.3.0, in progress. macOS first. Linux installs, but some parts are untested and
the project's own checks need macOS. No Windows.

## What it does for you

- **Picks the right helper.** Fixed rules choose the provider and model for each job: best quality
  first, then the lowest usage. An AI model never makes that choice.
- **Gets a second opinion.** A helper's result is checked by a helper from the other provider when one
  is available, then by your project's own tests and security checks.
- **Keeps an eye on every helper.** You can ask how a helper is doing or stop it. One that goes quiet
  for ten minutes, or runs out of time, is stopped for you.
- **Works on several projects at once.** Each project runs its own helpers and keeps its own history.
- **Keeps its records on your machine.** Nothing is sent anywhere or committed to your project.

## What you need

- A Mac (or Linux, with the limits above), Git, and Python 3.11 or later from Homebrew or python.org.
- Claude Code, the Codex CLI, or both, signed in with their subscriptions. COCOLO works with Claude
  Code 2.1 and Codex CLI 0.164 to 0.169.
- No API keys set in your terminal (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`). COCOLO uses subscription
  logins only.
- For a project you have not set up before: a small file that names its test and security commands.
  The [recipe](docs/manual-setup.md#operational-checks) takes a few minutes.

## Start

Clone this repository somewhere outside your projects, then run the setup wizard from it:

```sh
./setup.sh /path/to/your/project
```

The wizard checks your machine, asks which helpers you want, installs into the project and asks you
to sign one short statement about your billing. Use the arrow keys and Enter, press `?` to see what a
step did, and `q` to quit. Run it again any time to update, check or re-sign.

[Getting started](docs/getting-started.md) walks through a first run, step by step.

## What happens next

Open your project and ask for help in plain words:

- **In Claude Code:** "Use the `agent-orchestration` skill to have the routed reviewer check the error
  handling in `src/`."
- **In Codex:** start `codex` (or `codex -p orchestrator`, if setup made that profile) in the
  project's own folder, from your own terminal, and ask for the `orchestrate-agents` skill.

Your session takes it from there and reports back. If a job is refused, the answer says why and what
to do. Nearly every fix is "run `./setup.sh` again". More in [using it](docs/using-it.md).

> [!CAUTION]
> Open Codex on the project's own folder. Never open it on a folder that holds your projects, on this
> repository, on your home folder or on `/`. A session opened too high up could change files that run
> outside Codex's sandbox. [Why](docs/security-model.md#where-to-open-codex)

## Safety, in short

- **Only your subscriptions.** No helper starts until you have signed that your accounts cannot be
  charged beyond their subscriptions. An API key in your terminal, an unknown client version, or paid
  or unknown billing stops every helper. Sign again whenever your billing settings change.
- **Helpers only read.** Every helper is read-only and sees only the committed files it was given.
- **You stay in charge.** Setup and signing run this repository's code, never your project's, and only
  you can sign with your key.

The [security model](docs/security-model.md) has the exact rules and the known limits.

## Read more

- [Getting started](docs/getting-started.md): from a fresh clone to a first review
- [Using it](docs/using-it.md): asking for help from Claude Code and from Codex
- [Setup wizard](docs/setup-wizard.md): every step and choice
- [Troubleshooting](docs/troubleshooting.md) and [FAQ](docs/faq.md)
- [How it works](docs/how-it-works.md) and the [security model](docs/security-model.md)
- [All documentation](docs/README.md)

## What's next

Helpers that can edit files, each in its own copy of the project, are planned for release 0.4.0.
What changed in each release is in the [CHANGELOG](CHANGELOG.md).

## Contributing, security and license

- [CONTRIBUTING.md](CONTRIBUTING.md): how to contribute. Changes to safety or billing logic need an
  independent security review.
- [SECURITY.md](SECURITY.md): report vulnerabilities privately, not in public issues.
- [Apache License 2.0](LICENSE). See [NOTICE](NOTICE).
