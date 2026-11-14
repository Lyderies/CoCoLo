# COCOLO documentation

Start with [getting started](getting-started.md) if you have not set up COCOLO yet. The pages are
grouped by what you need from them.

## Learn

- [Getting started](getting-started.md): from a fresh clone to a first routed review in one project.

## Do

- [Using it](using-it.md): asking for help from Claude Code and from Codex, watching and stopping a
  child, reading a denial, and keeping it working.
- [Setup wizard](setup-wizard.md): every wizard step and choice, re-running, and where to open Codex.
- [Manual setup](manual-setup.md): the commands the wizard runs, including the operational checks
  recipe and how to remove everything.
- [Troubleshooting](troubleshooting.md): common messages and their fixes.

## Look up

- [Configuration](configuration.md): project files, machine files and environment variables.
- [Routing table](routing-table.md): which provider and model each task goes to, and how the table
  changes.

## Understand

- [How it works](how-it-works.md): primaries, children, admission, project services, run health,
  capacity, the records and the dispatch paths.
- [Security model](security-model.md): the threat model, the exact rules COCOLO enforces and what it
  does not protect against.
- [FAQ](faq.md): short answers to common questions.

## Contribute

- [CONTRIBUTING.md](../CONTRIBUTING.md): the branch model, the checks and the no-identifiers rule.
- [SECURITY.md](../SECURITY.md): how to report a vulnerability.
- [CHANGELOG.md](../CHANGELOG.md): what changed in each release.

## For developers

Design notes and implementation status for maintainers. You do not need them to use COCOLO, and they
describe work in progress, not how to set it up.

- [Local service API implementation status](service-api-development.md)
- [Native caller enrollment design](native-caller-enrollment-design.md)
- [Controller-owned primary bootstrap proposal](native-primary-controller-proposal.md)
- [Storage placement and migration plan](storage-placement-migration-plan.md)
