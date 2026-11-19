"""The wizard's terminal (S5, W9): screen, header, keys, menus, toggles and the text prompt, ported
from setup.sh's drawing and key functions. Output goes to `out`, keys come from `read`, so the same
code runs on a real terminal and under scripted tests."""

import os

STEP_NAMES = ("Checks", "Project", "Providers", "Install", "Collector", "Billing", "Profile", "Commands", "Done")
COLOUR_NAMES = ("reset", "bold", "dim", "red", "green", "yellow", "blue", "magenta", "cyan")
NO_COLOURS = dict.fromkeys(COLOUR_NAMES, "")
_MARKS = {"ok": ("OK  ", "green"), "fail": ("FAIL", "red"), "warn": ("WARN", "yellow"), "skip": ("SKIP", "yellow")}


class Stop(Exception):
    """The wizard stops: `code` is the exit status, `message` the line to print, if any."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def terminal_colours() -> dict:
    """setup.sh's setup_colors: none with NO_COLOR, no TERM or a dumb one, or fewer than 8 colours;
    otherwise the terminal's own sequences, read from terminfo without starting a program."""
    term = os.environ.get("TERM", "")
    if os.environ.get("NO_COLOR") or not term or term == "dumb":
        return dict(NO_COLOURS)
    try:
        import curses
        curses.setupterm(term, 1)
        if curses.tigetnum("colors") < 8:
            return dict(NO_COLOURS)
        setaf = curses.tigetstr("setaf") or b""

        def cap(name: str) -> str:
            return (curses.tigetstr(name) or b"").decode("latin-1")

        colours = {"reset": cap("sgr0"), "bold": cap("bold"), "dim": cap("dim")}
        for number, name in enumerate(("red", "green", "yellow", "blue", "magenta", "cyan"), start=1):
            colours[name] = curses.tparm(setaf, number).decode("latin-1") if setaf else ""
        return colours
    except Exception:
        return dict(NO_COLOURS)


def fold(text: str, width: int) -> str:
    """`fold -s -w WIDTH`: each line broken after the last blank that fits, else at the width."""
    lines = []
    for line in text.split("\n"):
        while len(line) > width:
            cut = line.rfind(" ", 0, width)
            if cut < 0:
                lines.append(line[:width])
                line = line[width:]
            else:
                lines.append(line[:cut + 1])
                line = line[cut + 1:]
        lines.append(line)
    return "\n".join(lines)


class Terminal:
    def __init__(self, *, out, read, colours, size, names=STEP_NAMES) -> None:
        self.out = out
        self._read = read  # read(count, timeout) -> bytes; b"" at end of input; None on timeout
        self.c = colours
        self._size = size
        self.names = names
        self.step = 0
        self.stat: dict[int, str] = {}
        self.body_text = ""
        self.items = ""
        self.step_log: list[str] = []
        self.in_details = False
        self.redraw = False  # Set by the resize handler.
        self.cols = 80

    # Drawing --------------------------------------------------------------------------------

    def write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def hide_cursor(self) -> None:
        self.write("\033[?25l")

    def show_cursor(self) -> None:
        self.write("\033[?25h")

    def body(self, text: str) -> None:
        self.body_text = f"{self.body_text}\n\n{text}" if self.body_text else text

    def list_add(self, kind: str, text: str) -> None:
        mark, colour = _MARKS.get(kind, ("  - ", "cyan"))
        self.items += f"  {self.c[colour]}{self.c['bold']}{mark}{self.c['reset']} {text}\n"

    def list_sub(self, text: str) -> None:
        self.items += f"       {self.c['dim']}{text}{self.c['reset']}\n"

    def list_cmd(self, text: str) -> None:
        self.items += f"    {text}\n"

    def list_plain(self, text: str) -> None:
        self.items += f"  {text}\n"

    def screen_reset(self) -> None:
        self.body_text = ""
        self.items = ""

    def term_size(self) -> None:
        cols, _lines = self._size()
        self.cols = max(int(cols or 80), 30)

    def header(self) -> str:
        c, cols, total = self.c, self.cols, len(self.names)
        text = f"{c['bold']}{c['magenta']} COCOLO setup {c['reset']}"
        if self.step > 0:
            text += f"{c['dim']} Step {self.step} of {total}{c['reset']}"
        text += "\n"
        if self.step > 0:
            sep = "  " if len("".join("  " + name for name in self.names)) < cols else " "
            plain = "".join(sep + name for name in self.names)
            line = ""
            for index, name in enumerate(self.names, start=1):
                state = self.stat.get(index, "")
                if index == self.step:
                    line += f"{sep}{c['cyan']}{c['bold']}[{name}]{c['reset']}"
                else:
                    colour = {"ok": "green", "fail": "red", "skip": "yellow"}.get(state, "dim")
                    line += f"{sep}{c[colour]}{name}{c['reset']}"
            if len(plain) < cols:
                text += line + "\n"
            else:
                text += f" {c['cyan']}{c['bold']}{self.names[self.step - 1]}{c['reset']}\n"
            width = min(cols - 4, 40)
            filled = self.step * width // total
            text += f" {c['green']}{'#' * filled}{'-' * (width - filled)}{c['reset']}\n"
        return text + f"{c['dim']}{'-' * (cols - 1)}{c['reset']}\n"

    def render(self) -> None:
        self.term_size()
        text = "\033[H\033[2J" + self.header() + "\n"
        if self.body_text:
            folded = fold(self.body_text + "\n\n", self.cols - 4)
            text += "".join(f"  {line}\n" for line in folded[:-1].split("\n"))
        if self.items:
            text += self.items + "\n"
        self.write(text)

    # Keys -----------------------------------------------------------------------------------

    def _char(self, timeout):
        """One character; None on timeout, "" at end of input."""
        first = self._read(1, timeout)
        if first is None or first == b"":
            return None if first is None else ""
        need = 3 if first[0] >= 0xF0 else 2 if first[0] >= 0xE0 else 1 if first[0] >= 0xC0 else 0
        rest = b""
        while len(rest) < need:
            more = self._read(1, 1)
            if not more:
                break
            rest += more
        return (first + rest).decode("utf-8", "replace")

    def read_key(self) -> str:
        while True:
            char = self._char(1)
            if char is None:
                if self.redraw:
                    self.redraw = False
                    return "WINCH"
                continue
            break
        if char == "":
            return "EOF"
        if char == "\033":
            rest = ""
            for _ in range(2):
                more = self._char(1)
                if not more:
                    break
                rest += more
            if rest in ("[A", "OA"):
                return "UP"
            if rest in ("[B", "OB"):
                return "DOWN"
            if rest in ("[5", "[6"):
                self._char(1)
                return "PGUP" if rest == "[5" else "PGDN"
            return "ESC"
        if char in ("\n", "\r"):
            return "ENTER"
        if char == " ":
            return "SPACE"
        return char

    def confirm_quit(self) -> None:
        self.write(f"\n  {self.c['yellow']}Quit setup? Nothing further will change. [y/N] {self.c['reset']}")
        self.show_cursor()
        answer = self._char(None)
        self.hide_cursor()
        if answer in ("y", "Y"):
            self.write("\n")
            self.show_cursor()
            raise Stop(130, "Setup stopped. Run ./setup.sh again any time.")

    def _closed(self) -> None:
        self.show_cursor()
        raise Stop(1, "Input closed; stopping.")

    # Menus ----------------------------------------------------------------------------------

    def menu(self, default: int, items: list[str]) -> int:
        selected, count = default, len(items)
        self.hide_cursor()
        while True:
            self.render()
            width = self.cols - 8
            rows = ""
            for index, item in enumerate(items):
                if index == selected:
                    rows += f"   {self.c['cyan']}{self.c['bold']}> {item[:width]}{self.c['reset']}\n"
                else:
                    rows += f"     {item[:width]}\n"
            hint = "Enter go back   q quit" if self.in_details else "Up/Down move   Enter select   ? what ran   q quit"
            self.write(rows + f"\n  {self.c['dim']}{hint}{self.c['reset']}\n")
            key = self.read_key()
            if key in ("UP", "k"):
                selected = (selected + count - 1) % count
            elif key in ("DOWN", "j"):
                selected = (selected + 1) % count
            elif key == "?" and not self.in_details:
                self.in_details = True
                try:
                    self.show_details()
                finally:
                    self.in_details = False
                self.hide_cursor()
            elif key == "ENTER":
                self.show_cursor()
                return selected
            elif key in ("q", "Q"):
                self.confirm_quit()
            elif key == "EOF":
                self._closed()

    def pause(self) -> None:
        self.menu(0, ["Continue"])

    def show_details(self) -> None:
        """W9.5: the ? screen: what ran in this step, in order, never command output."""
        saved = self.body_text, self.items
        self.body_text, self.items = "What ran in this step, in order:", ""
        for line in self.step_log or ["Nothing has run in this step yet."]:
            self.list_add("info", line)
        self.list_sub("")
        self.list_sub("A reason in brackets is the orchestrator's own code; docs/troubleshooting.md explains each one.")
        self.list_sub("Command output is kept private: it can hold account details, so it is not shown here.")
        self.menu(0, ["Back"])
        self.body_text, self.items = saved

    def toggles(self, state: str, items: list[str]) -> str:
        selected, count = 0, len(items)
        self.hide_cursor()
        while True:
            self.render()
            width = self.cols - 12
            rows = ""
            for index, item in enumerate(items):
                mark = "[x]" if state[index] == "1" else "[ ]"
                if index == selected:
                    rows += f"   {self.c['cyan']}{self.c['bold']}> {mark} {item[:width]}{self.c['reset']}\n"
                else:
                    rows += f"     {mark} {item[:width]}\n"
            self.write(rows + f"\n  {self.c['dim']}Up/Down move   Space toggle   Enter accept   q quit{self.c['reset']}\n")
            key = self.read_key()
            if key in ("UP", "k"):
                selected = (selected + count - 1) % count
            elif key in ("DOWN", "j"):
                selected = (selected + 1) % count
            elif key == "SPACE":
                state = state[:selected] + ("0" if state[selected] == "1" else "1") + state[selected + 1:]
            elif key == "ENTER":
                self.show_cursor()
                return state
            elif key in ("q", "Q"):
                self.confirm_quit()
            elif key == "EOF":
                self._closed()
