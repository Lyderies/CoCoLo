"""The wizard's pure decisions (S5): no terminal, no process, no file. Each ports setup.sh exactly."""

import re

_MODEL = re.compile(r"[A-Za-z0-9._-]+")


class ChoiceError(ValueError):
    """A choice the wizard refuses; its message is fixed text, safe to show."""


def expand_home(value: str, home: str) -> str:
    """W3.1: strip surrounding spaces and one pair of matching quotes, then expand a leading `~`,
    `$HOME` or `${HOME}`. Anything else (`~other`, a `~` that is not leading, `$HOMEWORK`) stays."""
    value = value.strip(" \t\n\r\f\v")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    for marker in ("~", "$HOME", "${HOME}"):
        if value == marker:
            return home
        if value.startswith(marker + "/"):
            return home + "/" + value[len(marker) + 1:]
    return value


def enabled_providers(routing_text: str) -> list[str]:
    """The providers a provider-routing.json enables, or [] when it cannot be read as one."""
    import json
    try:
        providers = json.loads(routing_text)["providers"]
        return [name for name in ("codex", "claude")
                if type(providers.get(name)) is dict and providers[name].get("enabled") is True]
    except (ValueError, TypeError, KeyError, AttributeError):
        return []


def billing_current(routing_text: str, observed: dict, status) -> bool:
    """S4-3: whether an update may skip the attestation. Only when every provider the routing enables
    was just observed (so its account is the current one) and the status says that account is attested;
    anything missing, failed or malformed means the attestation runs as before."""
    enabled = enabled_providers(routing_text)
    if not enabled or type(status) is not dict or status.get("ready") is not True:
        return False
    attestations = status.get("provider_attestations")
    if type(attestations) is not dict:
        return False
    for name in enabled:
        entry = attestations.get(name)
        if observed.get(name) is not True or type(entry) is not dict or entry.get("state") != "present":
            return False
    return True


def provider_routing(*choices: str) -> str:
    """W4.1-W4.3: the provider routing for four choices (Codex, Claude, Claude models, bridging), byte
    for byte as setup.sh's routing_json writes it."""
    if len(choices) != 4:
        raise ChoiceError("four choices are needed: codex, claude, models, bridge")
    codex, claude, models, bridge = choices
    for name, value in (("codex", codex), ("claude", claude), ("bridge", bridge)):
        if value not in ("on", "off"):
            raise ChoiceError(f"{name} must be on or off")
    if codex == "off" and claude == "off":
        raise ChoiceError("at least one provider must be enabled")
    if bridge == "on" and claude == "off":
        raise ChoiceError("bridging to Claude needs Claude children enabled")
    out = '{\n  "version": 1,\n  "providers": {\n    "codex": {"enabled": '
    out += "true" if codex == "on" else "false"
    out += '},\n    "claude": {"enabled": '
    if claude == "on":
        if models in ("-", ""):
            raise ChoiceError("choose at least one Claude model")
        names = models.split(",")
        if any(name == "" for name in names):
            raise ChoiceError("invalid model list: empty model name")
        for name in names:
            if not _MODEL.fullmatch(name):
                raise ChoiceError("invalid model name: " + name)
        out += 'true, "models": [' + ", ".join(f'"{name}"' for name in names) + "]"
    else:
        out += "false"
    out += '}\n  },\n  "codex_primary_may_bridge_to_claude": '
    out += "true" if bridge == "on" else "false"
    return out + "\n}\n"


PROFILE_LIMIT = 65536  # Bytes of the orchestrator profile read; a larger file is unreadable.


def profile_difference(raw: bytes | None) -> str:
    """W8.1: compare the settings the wizard manages, not the bytes: "same", the first differing setting
    ("sandbox_mode", "network_access"), or "unreadable". Never returns contents."""
    try:
        import tomllib
        if raw is None or len(raw) > PROFILE_LIMIT:
            raise ValueError
        data = tomllib.loads(raw.decode("utf-8"))
        box = data.get("sandbox_workspace_write")
        box = box if isinstance(box, dict) else {}
        if data.get("sandbox_mode") != "workspace-write":
            return "sandbox_mode"
        if box.get("network_access") is not True:
            return "network_access"
        return "same"
    except Exception:
        return "unreadable"


def profile_network_on(raw: bytes | None) -> bool:
    """W4.6: whether `network_access = true` is the last such setting inside `[sandbox_workspace_write]`,
    comments ignored, read line by line as setup.sh does (no TOML parser needed)."""
    section, network = "", False
    for line in (raw or b"")[:PROFILE_LIMIT].decode("utf-8", "replace").splitlines():
        compact = re.sub(r"\s+", "", line.split("#", 1)[0])
        if compact.startswith("[") and compact.endswith("]"):
            section = compact[1:-1]
        elif section == "sandbox_workspace_write" and compact == "network_access=true":
            network = True
        elif section == "sandbox_workspace_write" and compact == "network_access=false":
            network = False
    return network


def codex_hint(wanted: bool, ready: bool, network_on: bool) -> tuple[str, str]:
    """W4.6: the command that starts a Codex session able to reach Claude children, and why."""
    if ready or network_on:
        return ("codex -p orchestrator",
                "The profile turns network on for these sessions, which Claude children need.")
    if wanted:
        return ("codex",
                "The orchestrator profile is missing or does not set network_access = true under "
                "[sandbox_workspace_write], so Codex sessions cannot reach Claude children yet.")
    return "codex", "Codex-to-Claude bridging was not chosen, so no profile is needed."


def routing_bridges(raw: bytes | None) -> bool:
    """Whether a provider-routing.json lets Codex sessions bridge to Claude, read as JSON (review
    040-s1-update: whitespace never decides it). Not valid JSON is no."""
    try:
        import json
        data = json.loads(raw)
        return isinstance(data, dict) and data.get("codex_primary_may_bridge_to_claude") is True
    except Exception:
        return False


def codex_primary(raw: bytes | None) -> bool:
    """W4.5: whether a Codex session may orchestrate: no routing file means only Codex is enabled (yes);
    otherwise Codex children enabled, or Codex sessions allowed to bridge to Claude. A file that is
    not valid JSON is no. (Refusing a symlink or FIFO is the effect that reads the file.)"""
    if raw is None:
        return True
    try:
        import json
        data = json.loads(raw)
        codex = data.get("providers", {}).get("codex", {}).get("enabled")
        return codex is True or data.get("codex_primary_may_bridge_to_claude") is True
    except Exception:
        return False


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_REGISTRATION_FAILURES = {
    "ledger_upgrade_failed": "Your records were not upgraded: they are as the earlier version left them.",
    "ledger_upgrade_capacity": "Your records were not upgraded and nothing was changed: their backups do not fit.",
    "registration_incomplete_after_upgrade": "Your records were upgraded, but registering this project did not "
                                             "finish. Run setup again and repeat this step.",
    "ledger_upgrade_unknown": "The upgrade stopped and the records could not be read afterwards. Delete nothing.",
    "registration_incomplete_after_move": "Registering this project did not finish. Run setup again and repeat "
                                          "this step.",
    "collector_not_started_after_registration": "This project is registered, but the collector did not start "
                                                "again. Run setup again and repeat this step.",
}


def plain(value) -> str:
    """A value from a reply, safe to show: text only, every control character removed."""
    return _CONTROL.sub("", value) if isinstance(value, str) else ""


def registration_outcome(reply: dict, moved: str | None) -> tuple[bool, list[str]]:
    """W6.2, W6.5: whether learning-register succeeded, and the lines the collector step shows. One
    fixed sentence per failure reason, never one that misdescribes how far it got; paths and causes
    shown with control characters removed. `moved` is the old records' path when they just moved."""
    lines = []
    copy = plain(reply.get("ledger_copy"))
    if reply.get("ready") is not True:
        lines.append("Collector registration failed")
        reason = reply.get("reason_code")
        if reason in _REGISTRATION_FAILURES:
            lines.append(_REGISTRATION_FAILURES[reason])
        if reason == "ledger_upgrade_failed":
            cause = plain(reply.get("cause"))
            if cause:
                lines.append(f"What stopped it: {cause}.")
        elif reason == "ledger_upgrade_capacity":
            error = plain(reply.get("error"))
            lines.append(error.removeprefix("the records were not upgraded and nothing was changed: ") + ".")
        if copy.startswith("/"):
            lines.append(f"A copy of the records as they were before the upgrade is at {copy}.")
        state = plain(reply.get("state_moved"))
        if state.startswith("/"):
            lines.append(f"Your records were moved to the protected folder first and stay there; the old copy is "
                         f"at {state}. The earlier version's collector was not started again.")
        return False, lines
    lines.append("Collector registered")
    if copy.startswith("/"):
        lines.append(f"Upgraded your orchestrator records. A copy of them as they were is at {copy}; "
                     "delete it once you're happy.")
    moved = plain(moved)
    if moved.startswith("/"):
        lines.append("Moved your orchestrator records once to a protected folder, ~/.local/state/codex-agents. "
                     f"The old copy at {moved} can be deleted once you're happy.")
    return True, lines


def project_choices(lines, default, orchestrator, has_install, is_git_top) -> tuple[list[str], list[str]]:
    """W3.2: the remembered projects that still hold an install (absolute, no control character), then
    this folder unless it is listed already, is the orchestrator itself, or is not a Git top folder.
    Returns ([], []) when nothing is remembered, and the wizard asks for a path instead."""
    paths = [line for line in lines
             if line.startswith("/") and not _CONTROL.search(line) and has_install(line)]
    if not paths:
        return [], []
    items = list(paths)
    if default and default not in paths and default != orchestrator and is_git_top(default):
        paths.append(default)
        items.append(f"{default} (this folder)")
    return paths, items + ["Another folder...", "Quit"]


def remembered(lines, chosen) -> list[str] | None:
    """W3.2: the remembered list with `chosen` first and at most nine others; None when `chosen` is not
    an absolute path free of control characters (it is then not remembered)."""
    if not chosen.startswith("/") or _CONTROL.search(chosen):
        return None
    return [chosen] + [line for line in lines if line != chosen][:9]


def cannot_open(expanded: str, typed: str, guess: str | None) -> list[str]:
    """W3.4: what a path that cannot be opened says: the path, what was typed when it differs, how to
    type one, and the home-folder path when the leading ~ was lost and that path exists."""
    lines = [f"Cannot open: [{expanded}]"]
    if typed != expanded:
        lines.append(f"You typed: [{typed}]")
    lines.append("Type the full path, for example /Users/<you>/projects/app. Tab completes paths.")
    if guess:
        lines.append(f"That path does not exist, but this one does: {guess}")
    return lines


def failure_line(step: int, step_name: str, label: str, exit_code: int, hint: str) -> str:
    """W10.4: one failure, as the saved summary holds it: step, label, exit code and a fixed hint only."""
    return f"step {step} ({step_name}): {label} | exit code {exit_code} | {hint}"


def summary_text(failures: list[str]) -> str:
    """W10.4: the saved summary: a fixed heading and the failure lines, never command output."""
    return "COCOLO setup: what failed\n" + "".join(line + "\n" for line in failures)


def summary_destination_refusal(path: str) -> str | None:
    """W10.3: why a summary destination is refused, or None."""
    if _CONTROL.search(path):
        return "That location has a control character in it (such as a newline); nothing written"
    return None


# The Claude package's own files, exactly (the manifest and the helper modules included): the only
# files setup's advice ever tells anyone to delete.
CLAUDE_PACKAGE_FILES = (
    ".claude/agents/claude_agents_implementation_worker.md", ".claude/agents/claude_agents_planner.md",
    ".claude/agents/claude_agents_researcher.md", ".claude/agents/claude_agents_security_reviewer.md",
    ".claude/agents/claude_agents_verification_worker.md", ".claude/skills/claude-agents/SKILL.md",
    ".claude/claude-agents/install-manifest.json", ".claude/claude-agents/lib/claude_agents/__init__.py",
    ".claude/claude-agents/lib/claude_agents/contracts.py", ".claude/claude-agents/lib/claude_agents/validation.py",
)
CLAUDE_CONTROL_FILES = ".claude/claude-agents/install-pending.json and .claude/claude-agents/install.lock"
_LOOK_AND_SAVE = "Run git status and git diff -- .claude in the project to see what differs. Save any change you want to keep,"
_REMOVE_EXACT = ("remove the package files themselves, exactly these: " + ", ".join(CLAUDE_PACKAGE_FILES)
                 + " (remove a directory above them only if it is then empty), and commit that")

# setup.sh's explain_installer, in its order: the first pattern that matches gives the fixed message.
# The installer's own text is only matched, never shown.
_INSTALLER_REFUSALS = (
    (("*holds files the orchestrator does not install*",),
     "The project's .codex/orchestration holds files the orchestrator does not install, which code running "
     "outside Codex's sandbox could load (a Codex session may have planted them). Remove them, then run setup "
     "again. The installer names them: {manual}"),
    (("*xdg_state_home must be inside a hidden folder*",),
     "XDG_STATE_HOME must be inside a hidden folder in your home, such as ~/.local/state. Unset it or move it, "
     "then run setup again."),
    (("*a folder above the learning state*",),
     "A folder above your orchestrator records (such as ~/.local or ~/.local/state) is a symbolic link, is not "
     "a folder, belongs to another user or can be changed by other users. Fix it, or set XDG_STATE_HOME to "
     "another hidden folder in your home, then run setup again."),
    (("*cannot install the learning hooks*",),
     "The learning hooks could not be written: the project's folder path holds a quote, a backslash or a "
     "control character, or this Python is not in a trusted installation folder. Move the project, or run "
     "setup with a Python from Homebrew, python.org or the system."),
    (("*learning hook*",),
     "The project's learning hooks are missing, from an older release or edited by hand (Codex runs them "
     "outside its sandbox). Update the install (choose Configure, then 'Update the install and walk through every step')."),
    (("*merge*progress*", "*rebase*progress*", "*cherry-pick*progress*", "*revert*progress*",
      "*bisect*progress*", "*unmerged*"),
     "A Git operation (merge, rebase, cherry-pick or similar) is unfinished in the project. Finish or abort it "
     "first."),
    (("*no_write_report*",),
     "A package step ended without a complete, usable report of what it wrote (no write report), so setup stopped "
     "there. Files whose changes could not be accounted for were left as they are; the summary lists any undo. Run git status "
     "and git diff in the project to see what differs, put back with git what you do not want, and run setup "
     "again."),
    (("*previous_release_not_pinned*", "*previous_release_path_refused*", "*invalid_previous_release*"),
     "This copy of CoCoLo carries a list of earlier releases that failed its safety check, so setup will not "
     "use it to upgrade your project. Nothing was changed. Get a fresh copy of CoCoLo from its official "
     "source, then run setup again from that copy."),
    (("*untrusted_manifest*",),
     "The Claude files installed in this project do not match any release this setup knows how to "
     "upgrade (untrusted manifest). Nothing was changed. " + _LOOK_AND_SAVE + " then either put the package "
     "files back from Git, or " + _REMOVE_EXACT + ", and run setup again."),
    (("*update_required*",),
     "The Claude files installed in this project are from a different release than this setup (update "
     "required). Nothing was changed. Update the install (choose Configure, then 'Update the install and "
     "walk through every step')."),
    (("*managed_file_modified*", "*owned_file_changed*"),
     "A Claude package file in this project is changed or missing (managed file modified), so setup did "
     "not replace it. Nothing was changed. " + _LOOK_AND_SAVE + " then put that file back with git restore -- "
     "followed by its path, or " + _REMOVE_EXACT + ", and run setup again."),
    (("*installation_pending*", "*rollback_pending*"),
     "A Claude package change in this project was started and did not finish (installation pending), so some "
     "package files may be half written. " + _LOOK_AND_SAVE + " then put the package files back from Git, "
     "delete the two control files " + CLAUDE_CONTROL_FILES + ", and run setup again. Delete them only "
     "when no setup or install is running."),
    (("*not_installed*",),
     "The Claude package is not installed in this project yet (not installed), so there is nothing to update "
     "or verify. Nothing was changed. Run setup to install it."),
    (("*operation_failed*",),
     "The Claude package could not write all of its files (operation failed) and put back the ones it had "
     "already written. Nothing was changed. Check that the project folder is writable and has free space, then "
     "run setup again."),
    (("*write_failed*",),
     "The Claude package could not write its control file (write failed) before changing any of its files, so "
     "it had nothing to put back. Nothing was changed. Check that the project folder is writable and has free "
     "space, then run setup again."),
    (("*invalid_operation*", "*invalid_apply*"),
     "Setup called the Claude package with an operation or option it does not accept (invalid operation), "
     "which is a fault in setup itself, not in your project. Nothing was changed. Get a fresh copy of CoCoLo "
     "from its official source and run setup again from it; if it happens again, report it."),
    (("*invalid_source*", "*source_missing*", "*non_offline_helper*", "*role_model_missing*"),
     "This copy of CoCoLo's Claude package is incomplete or damaged, so setup will not use it. Nothing was "
     "changed. Get a fresh copy of CoCoLo from its official source, then run setup again from that copy."),
    (("*managed paths*", "*uncommitted*"),
     "Managed paths (.codex, .agents, .claude) have uncommitted changes. Commit or discard them first."),
    (("*top of its working tree*", "*not a usable git*", "*git_target_required*"),
     "The path is not the top folder of a Git working tree. Choose the project root."),
    (("*oversized_file*",),
     "A file at a Claude package path in this project is larger than any package file can be (oversized file), "
     "so setup did not read it. Nothing was changed. " + _LOOK_AND_SAVE + " then put that file back with git "
     "restore -- followed by its path, or remove it, and run setup again."),
    (("*project_folder_changed*",),
     "The project folder changed while setup was running, so setup stopped. Check which folder is now at the "
     "project path and run git status in both the original and replacement folders before running setup again."),
    (("*symlink*", "*unsafe_or_unavailable_path*", "*not a regular file*", "*invalid_root*", "*unsafe_file*",
      "*unsafe_path*", "*cannot be used safely*"),
     "A managed path is a symlink or cannot be used safely. Replace it with a real file or folder."),
    (("*differs*", "*conflict*", "*collision*", "*modified obsolete*", "*already exists*"),
     "A file in the project collides with a managed file (it differs from what would be installed). Move or "
     "commit it, then retry."),
    (("*permission rules*",),
     "The Claude Code permission rules that keep owner commands away from a Claude session are missing from "
     ".claude/settings.json. Update the install (choose Configure, then 'Update the install and walk through every step'), "
     "then commit."),
    (("*settings.json*", "*agent-orchestration-permissions*"),
     "The project's .claude/settings.json could not be merged safely: it must be a regular file holding a JSON "
     "object whose permissions.deny is a list. Fix it, commit, and retry."),
    (("*not an exact copy*",),
     "The orchestrator installed in this project is not an exact copy of this setup's version, so it was not "
     "run. Update the install, or restore the project's files with git."),
    (("*codex commands rule*",),
     "The Codex commands rule could not be removed by the installed orchestrator. Run the remove command "
     "shown in the summary yourself to see which file to delete."),
    (("*configuration is missing*", "*required*configuration*"),
     "Project configuration is missing (project-agent-routing.json or operational-checks.json)."),
    (("*not committed*", "*verification*"),
     "Verification failed: the project configuration and installed files must be committed and clean."),
)


NOT_UNDONE = ("Setup could not undo everything it had already changed, so some files may differ from before: "
               "check git status in the project before anything else.")


def rollback_incomplete(reply) -> bool:
    """True when an installer reply reports a rollback step that did not complete, or one that carries a note
    (something was left as it is or not undone): then setup never says "Nothing was changed"."""
    steps_ = reply.get("rollback") if isinstance(reply, dict) else None
    return isinstance(steps_, list) and any(not (isinstance(item, dict) and item.get("ok") is True)
                                            or isinstance(item.get("note"), str) for item in steps_)


def rollback_notes(reply) -> list[str]:
    """What the installer says about the steps it could not or would not undo, one note each."""
    steps_ = reply.get("rollback") if isinstance(reply, dict) else None
    return [item["note"] for item in steps_ or [] if isinstance(item, dict) and isinstance(item.get("note"), str)]


def installer_refusal(raw: str, manual: str, rollback_incomplete: bool = False) -> tuple[str, bool]:
    """W5.5, W10.4: the fixed message for an installer refusal, and whether it was recognised. The raw
    text is matched (case-sensitive, as bash's case is), never shown; an unknown refusal points to
    the command to run by hand."""
    from fnmatch import fnmatchcase
    for patterns, message in _INSTALLER_REFUSALS:
        if any(fnmatchcase(raw, pattern) for pattern in patterns):
            message = message.format(manual=manual)
            if rollback_incomplete:
                if patterns in (("*operation_failed*",), ("*write_failed*",)):
                    return ("The Claude package could not finish writing its files. " + NOT_UNDONE
                            + " Check that the project folder is writable and has free space, then run setup again."), True
                message = message.replace("Nothing was changed.", NOT_UNDONE)
                if NOT_UNDONE not in message:
                    message = f"{message} {NOT_UNDONE}"
            return message, True
    return "The installer refused: run it manually to see why:", False


def changed_paths(records: list[str] | None, candidates: list[str]) -> list[str]:
    """W5.1: the candidates that really changed, in Git's order (a rename's old and new names are both
    records). `records` is None when Git could not answer: then every candidate counts, so a failure
    never hides a needed commit."""
    if not candidates:
        return []
    if records is None:
        return list(candidates)
    wanted = set(candidates)
    return [name for name in records if name in wanted]


def uncommitted_display(records: list[str] | None, git_safe: bool) -> str:
    """W5.2: the uncommitted managed paths, one per line, control characters removed; or why they
    were not checked."""
    if not git_safe:
        return "(not checked: the project's Git settings name programs that Git would run)"
    if records is None:
        return "(not checked: Git could not read the project)"
    return "\n".join(plain(name) for name in records)


COMMIT_REFUSED_HINT = ("Git refused it. The usual cause is that your Git identity (user.name and user.email) is "
                       "not set. Commit the files yourself, then run ./setup.sh again and choose Verify.")

_COMMIT_NOTES = {
    "committed": "committed",
    "committed_stale": "committed; Git could not update the project's index (run git status and stage the committed "
                       "files yourself)",
    "nothing": "nothing to commit",
    "not_committed": "not committed (you chose to commit yourself)",
}


def commit_body(count: int) -> str:
    """W5.3: what the commit screen says before offering "Commit now"."""
    return (f"Next: commit the installed files in the project. Only these {count} paths are committed, nothing "
            "is pushed. Setup commits with Git's plumbing commands only (hash-object, update-index, write-tree, "
            "commit-tree, update-ref), which start no hook, filter, signing program, pager or maintenance "
            "whatever the project's Git settings say, and with every network transport off. To commit them "
            "yourself instead, these are the commands; choose 'Show every path' for the full list.")


def first_ten(paths: list[str], indent: str = "") -> list[str]:
    """The first ten of a list, then "... and N more" when there are others."""
    lines = [indent + path for path in paths[:10]]
    if len(paths) > 10:
        lines.append(f"{indent}... and {len(paths) - 10} more")
    return lines


def commit_outcome(state: str, uncommitted: str, ignored: int) -> tuple[str, str]:
    """W5.7, W10.5: the commit state after checking what is left, and the summary's commit line. A
    commit that left managed files uncommitted has failed; so has any run with ignored files."""
    if state == "committed" and uncommitted:
        state = "failed"
    if ignored > 0 and state != "failed":
        state = "failed"
    if state in _COMMIT_NOTES:
        return state, _COMMIT_NOTES[state]
    return state, "installed files ignored or could not be checked" if ignored > 0 else "commit failed"


def left_uncommitted(uncommitted: str, state: str) -> list[str]:
    """W5.7: the summary's list of what is left, shown unless the commit went through."""
    paths = [line for line in uncommitted.split("\n") if line]
    if not paths or state in ("committed", "committed_stale"):
        return []
    return ["Left uncommitted in the project:"] + first_ten(paths, "  ")


_SESSION_HOLDS = {
    "clone": "Codex was opened on a folder that holds this setup folder",
    "state": "Codex was opened on a folder that holds the orchestrator's learning state",
    "project": "Codex was opened on a folder that contains a set-up project",
}


def session_warnings(scan) -> list[str]:
    """W2.1-W2.4: one warning per session in the runtime's scan (its date, folder and why: the runtime's
    own message, else what the folder holds), then the advice once. Control characters are removed. A
    failed or empty scan warns about nothing; a session opened on a project itself is never in it."""
    items = scan.get("warnings") if isinstance(scan, dict) else None
    lines = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        date, holds, folder, message = (_CONTROL.sub("", str(item.get(key, "")))
                                        for key in ("date", "holds", "folder", "message"))
        if not folder:
            continue
        message = message or _SESSION_HOLDS.get(holds, "Codex was opened on a folder that holds your home folder")
        lines.append(f"A Codex session on {date} could write to {folder}. {message}.")
    if lines:
        lines.append("Open Codex on the project itself: never on a folder that contains a set-up project, this "
                     "setup folder, your home folder, a folder above it or /.")
    return lines


_RULE_STATUSES = ("installed", "missing", "stale", "unsupported")


def rule_status(value) -> str:
    """The host-command check's `codex_rule`, or "" for anything else."""
    return value if value in _RULE_STATUSES else ""


def commands_body(rule_file: str, status: str) -> list[str]:
    """W8.2, W8.4: what the commands-rule screen says before offering the rule."""
    lines = [
        "Codex runs each command in a sandbox, and that sandbox blocks the orchestrator's background service, so "
        "a Codex session cannot orchestrate this project on its own. This rule lets only the orchestrator's "
        "everyday commands for this project run outside the sandbox; owner commands, such as registration, "
        "billing attestation and promotion, stay blocked. To undo it later, use the remove command shown at "
        "the end of setup.",
        f"Every Codex session on this machine loads this rule from {rule_file} (mode 0600). It runs a protected "
        "copy of the orchestrator in ~/.codex that sessions opened on a project cannot change. Never open Codex "
        "on the orchestrator clone, a folder that contains a set-up project, your home folder, its parent or / "
        "while the rule exists.",
    ]
    if status == "stale":
        lines.append(
            "A rule for this project already exists and differs from this one, for example after a Python "
            "upgrade (the rule names the exact Python build) or a package update. It stays unchanged unless you "
            "replace it; until then Codex sessions may not reach the orchestrator.")
    return lines


def unsupported_home(reason: str) -> list[str]:
    """W8.6: why the rule cannot be used with this Codex home, and the fix."""
    return [
        "The Codex commands rule cannot be used with this Codex setup",
        "Reason: " + _CONTROL.sub(" ", reason or "")[:200],
        "The rule and its protected copy of the orchestrator work only in your account's own ~/.codex folder. "
        "Fix: run setup with HOME set to your own home folder, unset CODEX_HOME (or point it at ~/.codex), "
        "replace a symbolic link at ~/.codex with a real folder, then run ./setup.sh again.",
    ]


def verify_rule(status: str, reason: str) -> tuple[str, str, list[str]]:
    """W8.5, W8.6: what "Verify the install only" reports for the rule: step 8's mark and note, and
    its lines. It never writes the rule."""
    if status == "installed":
        return "ok", "installed", ["Codex commands rule: installed"]
    if status in ("missing", "stale"):
        return "skip", status, [
            f"Codex commands rule: {status}",
            "Codex sessions cannot reach the orchestrator without it. Run ./setup.sh, choose 'Update the "
            "install and walk through every step', and allow it in step 8."]
    if status == "unsupported":
        return "fail", "Codex home unsupported", unsupported_home(reason)
    return "fail", "not checked", [
        "Could not check the Codex commands rule",
        "The installed orchestrator may be older than this check. Update the install, then verify again."]


def get(reply, path: str) -> str:
    """setup.sh's `pj get`: a dotted path into a reply, formatted as the helper prints it (true, false,
    a list or object's length, text), "" when absent."""
    value = reply
    for key in path.split("."):
        if not key:
            continue
        if isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return ""
    if value is None:
        return ""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (list, dict)):
        return str(len(value))
    return str(value)


def reason(reply, fixed: bool) -> str:
    """setup.sh's `pj reason`: a refusal's reason code when it is a plain identifier, and its message
    only for the orchestrator CLI (`fixed`), whose messages are fixed strings."""
    if not isinstance(reply, dict):
        return ""
    code = next((reply[key] for key in ("reason_code", "account_reason_code", "code")
                 if isinstance(reply.get(key), str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reply[key])), "")
    error = reply.get("error") if isinstance(reply.get("error"), str) and fixed else ""
    error = re.sub(r"[^A-Za-z0-9 _.,:;'()/-]", "", error)[:160].strip()
    return " ".join(part for part in (error, f"({code})" if code else "") if part)


def raw(reply) -> str:
    """setup.sh's `pj raw`: every refusal text in an installer reply, lowercased on one line, only ever
    matched against fixed patterns, never shown."""
    reply = reply if isinstance(reply, dict) else {}
    parts = [reply[key] for key in ("refused", "error") if isinstance(reply.get(key), str)]
    for name in ("codex", "claude", "claude_settings", "codex_rule"):
        source = reply.get(name) or {}
        for key in ("errors", "project_errors", "package_errors"):
            parts.extend(item for item in source.get(key) or [] if isinstance(item, str))
        parts.extend(source[key] for key in ("error", "required_action") if isinstance(source.get(key), str))
    return " ".join(parts).lower().replace("\n", " ")


def verify_class(reply) -> str:
    """setup.sh's `pj verifyclass`: ready, linux-limited (only the operational-check sandbox, which
    needs macOS today, failed) or failed."""
    reply = reply if isinstance(reply, dict) else {}
    codex = reply.get("codex") or {}
    errors = codex.get("project_errors")
    limited = (codex.get("package_ready") is True and isinstance(errors, list) and errors
               and all(isinstance(item, str) and item.startswith("operational check sandbox preflight failed")
                       for item in errors)
               and (reply.get("claude") or {}).get("result") == "verified"
               and (reply.get("claude_settings") or {}).get("ready") is True)
    return "ready" if reply.get("ready") is True else ("linux-limited" if limited else "failed")


def _safe_word(value) -> str:
    return re.sub(r"[^A-Za-z0-9_ .+-]", "", str(value or ""))[:30] or "unknown"


def claude_login(raw_reply: bytes) -> tuple[bool, str, str]:
    """setup.sh's `pj claude`: whether Claude is signed in, its plan and login method as plain words."""
    try:
        import json
        data = json.loads(raw_reply)
    except Exception:
        return False, "", ""
    if isinstance(data, dict) and data.get("loggedIn") is True:
        return True, _safe_word(data.get("subscriptionType")), _safe_word(data.get("authMethod"))
    return False, "", ""


_QUOTED = set(" \t'\"\\|&;()<>!{}*[]?^$`,")


def shell_quote(value: str) -> str:
    """bash 3.2's `printf %q`: '' for nothing, $'...' when a control character is in it, else each
    shell-special character (and a leading #) behind a backslash."""
    if value == "":
        return "''"
    if _CONTROL.search(value):
        escapes = {"\n": "\\n", "\t": "\\t", "\r": "\\r", "\x1b": "\\E", "'": "\\'", "\\": "\\\\"}
        return "$'" + "".join(escapes.get(char, f"\\{ord(char):03o}" if _CONTROL.match(char) else char)
                              for char in value) + "'"
    return "".join("\\" + char if char in _QUOTED or (index == 0 and char == "#") else char
                   for index, char in enumerate(value))


def owner_key_notice(report) -> str:
    """W7.3: "mismatch" when the owner key file does not match its record, the time it was last
    replaced ("YYYY-MM-DD HH:MM"), or "" (setup.sh's owner_key_notice)."""
    if not isinstance(report, dict):
        return ""
    if report.get("state") == "mismatch":
        return "mismatch"
    if report.get("replacements"):
        try:
            import datetime
            return datetime.datetime.fromtimestamp(report["last_replaced_at_ms"] / 1000).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""
    return ""


def commit_status(status: int) -> tuple[str, str]:
    """The installer git tool's exit status as a commit state and, when there is one, what to tell the
    owner: 0 committed, 3 nothing to commit, 4 committed but the project's index was not updated."""
    if status == 0:
        return "committed", ""
    if status == 3:
        return "nothing", ""
    if status == 4:
        return "committed", ("Committed, but Git could not update the project's index. Run git status in the "
                             "project and stage the committed files yourself.")
    return "failed", ""


def status_warnings(reply) -> list[str]:
    """Plain status warnings for the terminal list, without paths or command output."""
    service = reply.get('project_service') if isinstance(reply, dict) else None
    warnings = service.get('warnings') if isinstance(service, dict) else None
    return [line for line in warnings if isinstance(line, str)] if isinstance(warnings, list) else []
