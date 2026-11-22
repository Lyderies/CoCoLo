"""COCOLO's setup wizard (S5). ./setup.sh trusts the interpreter, then runs this file with it:
`PY -I -B -S -X pycache_prefix=/dev/null scripts/setup-wizard.py [PROJECT_PATH | --print-... ARGS]`.

Isolated mode leaves this folder off sys.path, so this clone's own scripts folder is appended after
the standard library (W1.8); nothing is imported from the project or the current folder.
"""

import json
import os
import stat
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent))

from setup_wizard import effects, flow, steps, terminal  # noqa: E402


def _bool(value: str) -> bool:
    return value == "1"


def _nul(items) -> None:
    sys.stdout.buffer.write(b"".join(item.encode("utf-8", "surrogateescape") + b"\0" for item in items))


def _load(path):
    import json
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


class AnswersRefused(Exception):
    """An answers file that cannot be used; the message names the answer (S1-U3)."""


_ANSWER_KEYS = ("project", "operational_checks", "providers", "commit", "commands_rule", "codex_profile",
                "move_records", "upgrade_records", "billing_sign_again")
_PROVIDER_KEYS = ("codex", "claude", "claude_models", "bridge")


def read_answers(path: str) -> dict:
    """`--answers FILE`: every answer a fresh install reaches, checked before anything changes.

    Required: project, providers (codex, claude, claude_models, bridge), commit, commands_rule,
    move_records and upgrade_records; operational_checks (a non-empty path) unless the project already
    has one; codex_profile when bridging is on. Optional: billing_sign_again (true signs the billing
    statements again even when this project's attestation is already signed and current).
    """
    def refuse(key: str, why: str) -> AnswersRefused:
        return AnswersRefused(f"answers file: {key}: {why}. Nothing was changed.")
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65_536:
            raise refuse("file", "not a regular file of at most 64 KiB")
        with open(path, encoding="utf-8") as handle:
            answers = json.load(handle)
    except AnswersRefused:
        raise
    except (OSError, ValueError) as exc:
        raise refuse("file", f"cannot be read as JSON ({type(exc).__name__})") from None
    if not isinstance(answers, dict):
        raise refuse("file", "not a JSON object")
    for key in answers:
        if key not in _ANSWER_KEYS:
            raise refuse(key, "not a setup answer")
    # Every consent is stated up front, so nothing is refused after a change (review 040-s1-update).
    for key in ("project", "providers", "commit", "commands_rule", "move_records", "upgrade_records"):
        if key not in answers:
            raise refuse(key, "missing")
    if not isinstance(answers["project"], str) or not answers["project"]:
        raise refuse("project", "must be a path")
    project = answers["project"]
    if not os.path.isfile(os.path.join(project, ".codex", "orchestration", "operational-checks.json")):
        if not isinstance(answers.get("operational_checks"), str) or not answers["operational_checks"]:
            raise refuse("operational_checks", "missing" if "operational_checks" not in answers else "must be a path")
    providers = answers["providers"]
    if not isinstance(providers, dict):
        raise refuse("providers", "must be an object")
    for key in providers:
        if key not in _PROVIDER_KEYS:
            raise refuse(f"providers.{key}", "not a provider answer")
    for key in _PROVIDER_KEYS:
        if key not in providers:
            raise refuse(f"providers.{key}", "missing")
    for key in ("codex", "claude", "bridge"):
        if providers[key] not in ("on", "off"):
            raise refuse(f"providers.{key}", 'must be "on" or "off"')
    if not isinstance(providers["claude_models"], str):
        raise refuse("providers.claude_models", "must be a comma-separated list of models")
    models = providers["claude_models"] if providers["claude"] == "on" else "-"
    try:
        steps.provider_routing(providers["codex"], providers["claude"], models, providers["bridge"])
    except steps.ChoiceError as exc:
        raise refuse("providers", str(exc)) from None
    for key in ("commit", "commands_rule", "move_records", "upgrade_records", "billing_sign_again"):
        if key in answers and type(answers[key]) is not bool:
            raise refuse(key, "must be true or false")
    if providers["bridge"] == "on" and answers.get("codex_profile") not in ("create", "skip"):
        raise refuse("codex_profile", "missing" if "codex_profile" not in answers else 'must be "create" or "skip"')
    if "codex_profile" in answers and answers["codex_profile"] not in ("create", "skip"):
        raise refuse("codex_profile", 'must be "create" or "skip"')
    return answers


def helper(argv: list[str]) -> int:
    """The JSON helper operations the tests call (setup.sh's `--helper`)."""
    if argv[:1] == ["plan"] and len(argv) == 4:
        for kind, rel in effects.plan_entries(_load(argv[1]), argv[2], argv[3]):
            print(kind if rel is None else f"{kind} {rel}")
        return 0
    if argv[:1] == ["planpaths"] and len(argv) == 4:
        paths = effects.plan_paths(effects.plan_entries(_load(argv[1]), argv[2], argv[3]))
        if paths is None:
            return 3
        _nul(paths)
        return 0
    if argv[:1] == ["paths"] and len(argv) == 5:
        try:
            planned = [item.decode("utf-8") for item in Path(argv[3]).read_bytes().split(b"\0") if item]
        except (OSError, UnicodeDecodeError):
            return 3
        try:
            _nul(effects.commit_candidates(argv[2], _load(argv[1]) or {}, planned))
        except effects.EffectRefused as exc:
            return 3 if "unsafe" in str(exc) else 4
        return 0
    if argv[:1] == ["verifyclass"] and len(argv) == 2:
        print(steps.verify_class(_load(argv[1])))
        return 0
    if argv[:1] == ["profilecmp"] and len(argv) == 2:
        print(steps.profile_difference(effects.read_owned_file(argv[1])))
        return 0
    if argv[:1] == ["get"] and len(argv) == 3:
        print(steps.get(_load(argv[1]), argv[2]))
        return 0
    print("unknown helper", file=sys.stderr)
    return 2


def hidden(argv: list[str]) -> int | None:
    """setup.sh's hidden entry points for tests and scripts; None when argv is not one of them."""
    name, args = argv[0] if argv else "", argv[1:]
    usage = {
        "--print-provider-routing": (4, "--print-provider-routing CODEX CLAUDE MODELS BRIDGE"),
        "--write-provider-routing": (5, "--write-provider-routing PROJECT CODEX CLAUDE MODELS BRIDGE"),
        "--print-expanded-path": (1, "--print-expanded-path VALUE"),
        "--print-codex-hint": (2, "--print-codex-hint WANTED READY"),
        "--print-codex-primary": (1, "--print-codex-primary FILE"),
        "--print-uncommitted": (1, "--print-uncommitted PROJECT"),
        "--print-git-helpers": (1, "--print-git-helpers PROJECT"),
    }
    if name in usage and len(args) != usage[name][0]:
        print(f"usage: setup.sh {usage[name][1]}", file=sys.stderr)
        return 2
    if name in ("--print-changed-paths", "--print-ignored-paths") and not args:
        print(f"usage: setup.sh {name} PROJECT PATH...", file=sys.stderr)
        return 2
    home = os.environ.get("HOME", "")
    if name == "--print-provider-routing":
        try:
            sys.stdout.write(steps.provider_routing(*args))
        except steps.ChoiceError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    if name == "--write-provider-routing":
        try:
            effects.write_project_routing(args[0], steps.provider_routing(*args[1:]))
        except (steps.ChoiceError, effects.EffectRefused) as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0
    if name == "--print-expanded-path":
        print(steps.expand_home(args[0], home))
        return 0
    if name == "--print-codex-hint":
        profile = os.path.join(os.environ.get("CODEX_HOME") or os.path.join(home, ".codex"), "orchestrator.config.toml")
        command, why = steps.codex_hint(_bool(args[0]), _bool(args[1]),
                                        steps.profile_network_on(effects.read_owned_file(profile)))
        print(f"{command}\n{why}")
        return 0
    if name == "--print-codex-primary":
        status, raw = effects.read_routing(args[0])
        print("yes" if status != "refused" and steps.codex_primary(raw) else "no")
        return 0
    if name == "--print-uncommitted":
        helpers = effects.git_helpers(args[0])
        print(steps.uncommitted_display(effects.git_changes(args[0], list(flow.MANAGED)) if helpers == [] else None,
                                        helpers == []))
        return 0
    if name == "--print-changed-paths":
        records = effects.git_changes(args[0], args[1:]) if effects.git_helpers(args[0]) == [] else None
        _nul(steps.changed_paths(records, args[1:]))
        return 0
    if name == "--print-ignored-paths":
        found = effects.git_ignored(args[0], args[1:]) if effects.git_helpers(args[0]) == [] else None
        _nul(["!"] if found is None else found)
        return 0
    if name == "--print-git":
        print(effects.git_executable([Path.cwd()]) or "none")
        return 0
    if name == "--print-git-helpers":
        helpers = effects.git_helpers(args[0])
        print("none" if helpers == [] else " ".join(helpers or ["(the settings could not be read)"]))
        return 0
    if name == "--helper":
        return helper(args)
    return None


_HELD = False


def tty_hold():
    """Keep the terminal out of line mode and not echoing for a whole typed line (the line editor), so
    keys typed quickly are never line-buffered or echoed twice. TCSANOW: never waits for output."""
    import contextlib
    import termios

    @contextlib.contextmanager
    def held():
        global _HELD
        try:
            saved = termios.tcgetattr(0)
        except termios.error:
            yield
            return
        changed = termios.tcgetattr(0)
        changed[3] &= ~(termios.ICANON | termios.ECHO | termios.IEXTEN)  # IEXTEN: Ctrl-V reaches the wizard
        changed[6][termios.VMIN], changed[6][termios.VTIME] = 1, 0
        termios.tcsetattr(0, termios.TCSANOW, changed)
        _HELD = True
        try:
            yield
        finally:
            _HELD = False
            termios.tcsetattr(0, termios.TCSANOW, saved)
    return held()


def tty_read(count: int, timeout):
    """bash's `read -rsn1 -t`: one read with the terminal out of line mode and not echoing, restored
    after; None on timeout or a signal, b"" when the terminal is gone."""
    import select
    import termios
    if _HELD:
        try:
            ready, _, _ = select.select([0], [], [], timeout)
            return os.read(0, count) if ready else None
        except InterruptedError:
            return None
        except OSError:
            return b""
    try:
        saved = termios.tcgetattr(0)
    except termios.error:
        return b""
    changed = termios.tcgetattr(0)
    changed[3] &= ~(termios.ICANON | termios.ECHO | termios.IEXTEN)  # IEXTEN: Ctrl-V reaches the wizard
    changed[6][termios.VMIN], changed[6][termios.VTIME] = 1, 0
    try:
        termios.tcsetattr(0, termios.TCSANOW, changed)
        ready, _, _ = select.select([0], [], [], timeout)
        return os.read(0, count) if ready else None
    except InterruptedError:
        return None
    except OSError:
        return b""
    finally:
        try:
            termios.tcsetattr(0, termios.TCSANOW, saved)
        except termios.error:
            pass


def main(argv: list[str]) -> int:
    answered = hidden(argv)
    if answered is not None:
        return answered
    if argv[:1] in (["-h"], ["--help"]):
        print("Usage: ./setup.sh [PROJECT_PATH]\n       ./setup.sh --update PROJECT_PATH   (update an installed project "
              "with no menu; only the owner-key passphrase is asked)\n       ./setup.sh --answers FILE   (install from "
              "an answers file with no menu: docs/setup-wizard.md)\n\nInteractive wizard that installs COCOLO (Claude Orchestrator, Codex "
              "Orchestrator, Local Orchestrator) into a Git project.\nRun it from a terminal in the root of this "
              "repository. Keys: Up/Down move, Enter selects,\nSpace toggles, q quits. Set NO_COLOR=1 for plain output.")
        return 0
    if not (os.isatty(0) and os.isatty(1)):  # W9.1: a human step, refused without a terminal.
        print("setup.sh is interactive and must run in a terminal (stdin and stdout).\nThe billing attestation is a "
              "human step, so it cannot be scripted or piped.\nRun ./setup.sh from a terminal window. For manual "
              "commands, see docs/manual-setup.md.", file=sys.stderr)
        return 2
    import shutil
    import signal
    update = argv[:1] == ["--update"]
    scripted = argv[:1] == ["--answers"]
    if (update or scripted) and len(argv) != 2:
        print(f"usage: setup.sh {argv[0]} {'PROJECT_PATH' if update else 'ANSWERS_FILE'}", file=sys.stderr)
        return 2
    answers = None
    if scripted:
        try:
            answers = read_answers(argv[1])
        except AnswersRefused as refused:
            print(refused, file=sys.stderr)
            return 2
    start = (argv[1] if update else answers["project"] if scripted
             else os.getcwd() if not argv or argv[0].startswith("--") else argv[0])
    term = terminal.Terminal(out=sys.stdout, read=tty_read, colours=terminal.terminal_colours(),
                             size=lambda: tuple(shutil.get_terminal_size((80, 24))))
    term.hold = tty_hold
    wizard = flow.Wizard(term, start=start, python=os.environ.get("COCOLO_PYTHON") or sys.executable,
                         owner_path=os.environ.get("COCOLO_OWNER_PATH", ""),
                         bash_version=os.environ.get("COCOLO_BASH_VERSION", ""), update=update, answers=answers)

    def on_signal(signum, frame):
        term.write("\n")
        term.show_cursor()
        raise terminal.Stop(130)

    def on_resize(signum, frame):
        term.redraw = True

    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, on_signal)
    signal.signal(signal.SIGWINCH, on_resize)
    code = 0
    try:
        wizard.main()
    except terminal.Stop as stop:
        code = stop.code
        if stop.message:
            print(stop.message, file=sys.stderr if stop.stderr else sys.stdout)
    finally:
        wizard.cleanup()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
