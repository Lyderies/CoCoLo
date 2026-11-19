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
