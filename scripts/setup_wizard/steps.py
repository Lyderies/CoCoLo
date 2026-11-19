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
