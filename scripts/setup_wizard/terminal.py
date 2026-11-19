"""The wizard's terminal (S5, W9): screen, header, keys, menus, toggles and the text prompt, ported
from setup.sh's drawing and key functions. Output goes to `out`, keys come from `read`, so the same
code runs on a real terminal and under scripted tests."""

import os
import re
import signal
import subprocess
import time
from pathlib import Path

from . import steps

STEP_NAMES = ("Checks", "Project", "Providers", "Install", "Collector", "Billing", "Profile", "Commands", "Done")
COLOUR_NAMES = ("reset", "bold", "dim", "red", "green", "yellow", "blue", "magenta", "cyan")
NO_COLOURS = dict.fromkeys(COLOUR_NAMES, "")
_MARKS = {"ok": ("OK  ", "green"), "fail": ("FAIL", "red"), "warn": ("WARN", "yellow"), "skip": ("SKIP", "yellow")}


class Stop(Exception):
    """The wizard stops: `code` is the exit status, `message` the line to print, if any."""

    def __init__(self, code: int, message: str = "", *, stderr: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stderr = stderr  # setup.sh prints "Input closed" and the working-folder refusal there.


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


def terminal_width(columns, stty, tput) -> int:
    """S3-P5, S3-P9a: COLUMNS when set; else the terminal's own size (`stty size`'s "ROWS COLS", None
    when it failed); else the terminfo answer, trusted only when it succeeded ((ok, text)); a size of 0
    is not known. Not a plain decimal number: 80. Leading zeros are dropped; more than 4 digits: 9999."""
    width = columns or ""
    if not width:
        width = (stty or "").split(" ")[-1]
        if not re.search(r"[^0]", width):
            width = ""
    if not width:
        ok, text = tput
        width = text.strip() if ok else ""
        if not re.search(r"[^0]", width):
            width = ""
    if not re.fullmatch(r"[0-9]+", width):
        width = "80"
    width = width.lstrip("0")
    if not width:
        return 0
    return 9999 if len(width) > 4 else int(width)


def live_width(fd: int = 0) -> int:
    """The width of the wizard's own terminal (standard input) now, read again at each redraw (S3-P5)."""
    import fcntl
    import struct
    import termios
    try:
        rows, cols = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
        stty = f"{rows} {cols}"
    except OSError:
        stty = None
    try:
        tput = (True, str(os.get_terminal_size(2).columns))
    except OSError:
        tput = (False, "")
    return terminal_width(os.environ.get("COLUMNS"), stty, tput)


_PHASE_PREFIX = b"cocolo-phase: "
_SPINNER = "|/-\\"


def phase_of(path) -> str:
    """S3-P1, S3-P2, S3-P6: the last `cocolo-phase: ` line in the last 4 KiB of the error log, as ASCII
    letters, digits, spaces and ,.'- only, at most 60 characters."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(handle.tell() - 4096, 0))
            tail = handle.read().replace(b"\0", b"")
    except OSError:
        return ""
    lines = [line for line in tail.split(b"\n") if line.startswith(_PHASE_PREFIX)]
    if not lines:
        return ""
    text = lines[-1][len(_PHASE_PREFIX):][:200]
    return re.sub(rb"[^A-Za-z0-9 ,.'-]", b"", text).decode("ascii")[:60]


def complete_path(text: str, home: str) -> list[str]:
    """Tab completion for the path prompt (bash's `read -e`): the names starting with `text`, folders
    ending in "/", a leading ~ kept as typed."""
    import glob
    tilde = text == "~" or text.startswith("~/")
    expanded = home + text[1:] if tilde else text
    found = []
    for match in sorted(glob.glob(glob.escape(expanded) + "*")):
        shown = "~" + match[len(home):] if tilde else match
        found.append(shown + "/" if os.path.isdir(match) else shown)
    return found


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
        self.hold = None  # The real terminal's context that keeps it out of line mode for a whole line.

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
        cols, lines = self._size()
        self.cols = max(int(cols or 80), 30)
        self.lines = int(lines or 24)

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
        raise Stop(1, "Input closed; stopping.", stderr=True)

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

    def _line(self, complete=lambda text: []):
        """One typed line without its newline; None at end of input."""
        return self.edit_line(complete)

    def edit_line(self, complete) -> str | None:
        """W3.3: the prompt's own line editor (no libedit, which waits for the screen to drain): typing,
        Backspace, Ctrl-U, Ctrl-W, Ctrl-V for a literal character, Tab completion, Enter; Ctrl-D on an
        empty line or closed input ends input (None)."""
        import contextlib
        line = ""
        with self.hold() if self.hold is not None else contextlib.nullcontext():
            while True:
                char = self._char(None)
                if char == "":
                    return None
                if char in ("\n", "\r"):
                    self.write("\r\n")
                    return line
                if char == "\x04":
                    if not line:
                        return None
                elif char in ("\x7f", "\x08"):
                    if line:
                        line = line[:-1]
                        self.write("\b \b")
                elif char == "\x15":
                    self.write("\b \b" * len(line))
                    line = ""
                elif char == "\x17":
                    kept = line.rstrip(" ")
                    kept = kept[:kept.rfind(" ") + 1] if " " in kept else ""
                    self.write("\b \b" * (len(line) - len(kept)))
                    line = kept
                elif char == "\x16":
                    literal = self._char(None)
                    if literal == "":
                        return None
                    line += literal
                    self.write(literal if literal.isprintable() else "?")
                elif char == "\t":
                    import os.path
                    found = complete(line)
                    common = os.path.commonprefix(found) if found else ""
                    if len(common) > len(line) and common.startswith(line):
                        self.write(common[len(line):])
                        line = common
                elif char == "\033":
                    follow = self._char(1)
                    if follow in ("[", "O"):
                        while True:
                            final = self._char(1)
                            if not final or "@" <= final <= "~":
                                break
                elif char.isprintable():
                    line += char
                    self.write(char)

    def ask_text(self, prompt: str, default: str, *, home: str) -> tuple[str, str]:
        """setup.sh's ask_text: (the answer with home markers expanded, the answer as typed). Nothing
        typed takes the default; q asks to quit, then asks again."""
        while True:
            self.render()
            self.show_cursor()
            if default:
                self.write(f"  {self.c['bold']}{prompt}{self.c['reset']} [{default}]\n  > ")
            else:
                self.write(f"  {self.c['bold']}{prompt}{self.c['reset']} (type or paste the full path)\n  > ")
            typed = self._line(lambda text: complete_path(text, home))
            if typed is None:
                raise Stop(1, "Input closed; stopping.", stderr=True)
            answer = typed or default
            if answer in ("q", "Q"):
                self.confirm_quit()
                continue
            return steps.expand_home(answer, home), answer

    def view_text(self, text: str, title: str) -> None:
        """setup.sh's view_plan: TEXT in a scrollable view headed TITLE."""
        offset = 0
        self.hide_cursor()
        while True:
            self.term_size()
            lines = fold(text, self.cols - 6).split("\n")
            count = len(lines)
            page = max(self.lines - 9, 3)
            offset = max(min(offset, count - page), 0)
            shown = "".join(f"    {line}\n" for line in lines[offset:offset + page])
            self.write("\033[H\033[2J" + self.header()
                       + f"\n  {self.c['bold']}{title}: lines {offset + 1}-{min(offset + page, count)} of {count}"
                         f"{self.c['reset']}\n\n" + shown
                       + f"\n  {self.c['dim']}Up/Down scroll   PageUp/PageDown page   q back{self.c['reset']}\n")
            key = self.read_key()
            if key in ("UP", "k"):
                offset -= 1
            elif key in ("DOWN", "j"):
                offset += 1
            elif key == "PGUP":
                offset -= page
            elif key in ("PGDN", "SPACE"):
                offset += page
            elif key in ("q", "Q", "ENTER", "ESC", "EOF"):
                self.show_cursor()
                return

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

    # Running commands -----------------------------------------------------------------------

    def run_cmd(self, label: str, argv: list[str], *, logdir, slow_seconds: int = 10, width=live_width,
                quiet: bool = False, reason=None, draw=None) -> int:
        """setup.sh's run_cmd: run `argv` in its own process group with a spinner, the seconds so far,
        the command's phase and, after `slow_seconds`, that it can take a few minutes. Output goes to
        private logs in `logdir` (never shown), or nowhere when `quiet`. Returns the exit status."""
        self.command_count = getattr(self, "command_count", 0) + 1
        if quiet:
            self.last_out = self.last_err = None
            stdout = stderr = subprocess.DEVNULL
        else:
            self.last_out = str(Path(logdir) / f"cmd{self.command_count}.out")
            self.last_err = str(Path(logdir) / f"cmd{self.command_count}.err")
            stdout = os.open(self.last_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            stderr = os.open(self.last_err, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True, env={**os.environ, "COCOLO_PHASES": "1"})
        finally:
            if not quiet:
                os.close(stdout)
                os.close(stderr)
        self.active = process
        started, frame = time.monotonic(), 0
        try:
            if draw is not None:
                draw()  # After the start: a redraw that blocks never holds the command back.
            self.hide_cursor()
            while process.poll() is None:
                waited = int(time.monotonic() - started)
                hint = "  (still working: this can take a few minutes)" if waited >= slow_seconds else ""
                phase = "" if quiet else phase_of(self.last_err)
                columns = width()
                if columns > 6:
                    text = f"{label}  {waited}s" + (f"  {phase}" if phase else "") + hint
                    self.write(f"\r\033[K  {self.c['cyan']}{_SPINNER[frame % 4]}{self.c['reset']} "
                               f"{text[:columns - 6]} ")
                else:
                    self.write("\r\033[K")  # Too narrow for a frame: still erase the last one (S3-P9b).
                frame += 1
                time.sleep(0.15)
        except BaseException:
            self.stop_active()
            raise
        self.active = None
        status = process.returncode if process.returncode >= 0 else 128 - process.returncode
        self.last_rc = status
        self.write("\r\033[K")
        self.show_cursor()
        self.log_step(label, status, reason(self.last_out) if reason and status and not quiet else "")
        return status

    def stop_active(self) -> None:
        """Stop the running command's whole process group, then reap it."""
        process, self.active = getattr(self, "active", None), None
        if process is None:
            return
        for sig, pause in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 0)):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            time.sleep(pause)
        process.wait()

    def log_step(self, label: str, status: int, why: str = "") -> None:
        """One line for the ? screen: what ran and how it ended, and a refusal's fixed reason."""
        why = re.sub(r"[\x00-\x1f\x7f]", "", why or "")
        ended = "done" if status == 0 else f"stopped, exit {status}"
        self.step_log.append(f"{time.strftime('%H:%M:%S')}  {label}: {ended}" + (f" - {why}" if why else ""))
