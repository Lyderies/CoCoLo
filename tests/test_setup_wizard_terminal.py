"""S5: the Python wizard's terminal core (DESIGN-0.4.0-S5-wizard.md W9), ported from setup.sh.

The screen, header, keys, menus and toggles, driven by scripted keys into a string: no process and
no real terminal. Colours are off (NO_COLOR), so the expected text is setup.sh's own output.
"""

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from setup_wizard import terminal  # noqa: E402

NAMES = ("Checks", "Project", "Providers", "Install", "Collector", "Billing", "Profile", "Commands", "Done")


def keys(*items):
    """A key source that yields the given bytes one read at a time, then end of input."""
    data = bytearray(b"".join(item if isinstance(item, bytes) else item.encode() for item in items))

    def read(count, timeout):
        if not data:
            return b""
        chunk = bytes(data[:count])
        del data[:count]
        return chunk
    return read


def make(*pressed, cols=80):
    out = io.StringIO()
    term = terminal.Terminal(out=out, read=keys(*pressed), colours=terminal.NO_COLOURS, size=lambda: (cols, 24))
    return term, out


class FoldTests(unittest.TestCase):
    """`fold -s -w N`: break after the last blank that fits, else at the width."""

    def test_fold_breaks_after_the_last_blank_and_keeps_it(self) -> None:
        self.assertEqual("aaa bbb \nccc", terminal.fold("aaa bbb ccc", 8))
        self.assertEqual("abcdefgh\nij", terminal.fold("abcdefghij", 8))
        self.assertEqual("short", terminal.fold("short", 8))
        self.assertEqual("one\n\ntwo", terminal.fold("one\n\ntwo", 8))


class ScreenTests(unittest.TestCase):
    def test_lists_and_body_render_as_setup_sh_does(self) -> None:
        term, out = make()
        term.body("First paragraph.")
        term.body("Second.")
        term.list_add("ok", "Done thing")
        term.list_add("fail", "Broken thing")
        term.list_add("warn", "Careful")
        term.list_add("skip", "Skipped")
        term.list_add("info", "Note")
        term.list_sub("detail")
        term.render()
        text = out.getvalue()
        self.assertTrue(text.startswith("\033[H\033[2J COCOLO setup \n" + "-" * 79 + "\n\n"))
        self.assertIn("  First paragraph.\n  \n  Second.\n  \n", text)
        self.assertTrue(text.endswith("  OK   Done thing\n  FAIL Broken thing\n  WARN Careful\n  SKIP Skipped\n"
                                      "    -  Note\n       detail\n\n"))

    def test_the_header_shows_the_step_strip_and_bar(self) -> None:
        term, out = make()
        term.step = 1
        term.stat = {}
        term.render()
        lines = out.getvalue().split("\n")
        self.assertEqual("\033[H\033[2J COCOLO setup  Step 1 of 9", lines[0])
        self.assertEqual(" [Checks] Project Providers Install Collector Billing Profile Commands Done", lines[1])
        self.assertEqual(" " + "#" * 4 + "-" * 36, lines[2])
        self.assertEqual("-" * 79, lines[3])

    def test_a_narrow_header_names_only_the_current_step(self) -> None:
        term, out = make(cols=40)
        term.step = 4
        term.render()
        lines = out.getvalue().split("\n")
        self.assertEqual(" Install", lines[1])
        self.assertEqual(" " + "#" * 16 + "-" * 20, lines[2])
        self.assertEqual("-" * 39, lines[3])

    def test_a_reset_clears_body_and_list(self) -> None:
        term, out = make()
        term.body("x")
        term.list_add("ok", "y")
        term.screen_reset()
        term.render()
        self.assertNotIn("x", out.getvalue().split("\n", 3)[-1].replace("-", ""))


class KeyTests(unittest.TestCase):
    def test_keys_decode_as_setup_sh_reads_them(self) -> None:
        term, _ = make("\033[A", "\033OA", "\033[B", "\033OB", "\033[5~", "\033[6~", "\n", " ", "q", "\033x")
        self.assertEqual(["UP", "UP", "DOWN", "DOWN", "PGUP", "PGDN", "ENTER", "SPACE", "q", "ESC", "EOF"],
                         [term.read_key() for _ in range(11)])


class MenuTests(unittest.TestCase):
    def test_a_menu_moves_wraps_and_selects(self) -> None:
        term, out = make("\033[B", "j", "j", "k", "\n")  # 0 -> 1 -> 2 -> 0 (wraps) -> 2
        self.assertEqual(2, term.menu(0, ["One", "Two", "Three"]))
        last = out.getvalue().rsplit("\033[H\033[2J", 1)[1]
        self.assertIn("     One\n     Two\n   > Three\n\n  Up/Down move   Enter select   ? what ran   q quit\n", last)

    def test_W1_a_long_item_keeps_its_beginning_and_end_so_two_stay_apart(self) -> None:
        base = "/srv/work/projects/clients/" + "x" * 60
        items = [base + "/alpha-service", base + "/bravo-service", "Short"]
        term, out = make("\n", cols=60)
        term.menu(0, items)
        last = out.getvalue().rsplit("\033[H\033[2J", 1)[1]
        rows = [line for line in last.splitlines() if line.startswith(("     ", "   > "))][:3]
        shown = [row.strip().lstrip("> ").strip() for row in rows]
        self.assertEqual(60 - 8, len(shown[0]))
        self.assertNotEqual(shown[0], shown[1])
        self.assertTrue(shown[0].startswith("/srv/work/") and shown[0].endswith("/alpha-service"), shown[0])
        self.assertIn("...", shown[0])
        self.assertEqual("Short", shown[2])

    def test_quit_asks_first_and_y_stops_with_130(self) -> None:
        term, out = make("q", "n", "q", "y")
        with self.assertRaises(terminal.Stop) as stopped:
            term.menu(0, ["One"])
        self.assertEqual(130, stopped.exception.code)
        self.assertEqual("Setup stopped. Run ./setup.sh again any time.", stopped.exception.message)
        self.assertIn("Quit setup? Nothing further will change. [y/N] ", out.getvalue())

    def test_closed_input_stops_with_1(self) -> None:
        term, _ = make()
        with self.assertRaises(terminal.Stop) as stopped:
            term.menu(0, ["One"])
        self.assertEqual((1, "Input closed; stopping."), (stopped.exception.code, stopped.exception.message))

    def test_question_mark_shows_what_ran_then_returns_to_the_same_screen(self) -> None:
        """W9.5."""
        term, out = make("?", "\n", "\n")
        term.step_log = ["10:00:00  Installing: done"]
        term.body("The screen")
        self.assertEqual(0, term.menu(0, ["Continue"]))
        text = out.getvalue()
        self.assertIn("What ran in this step, in order:", text)
        self.assertIn("    -  10:00:00  Installing: done", text)
        self.assertIn("Command output is kept private", text)
        self.assertIn("  Enter go back   q quit", text)
        self.assertIn("The screen", text.rsplit("\033[H\033[2J", 1)[1])

    def test_toggles_flip_with_space_and_accept_with_enter(self) -> None:
        term, out = make(" ", "j", " ", "\n")
        self.assertEqual("01", term.toggles("10", ["Codex", "Claude"]))
        last = out.getvalue().rsplit("\033[H\033[2J", 1)[1]
        self.assertIn("     [ ] Codex\n   > [x] Claude\n\n  Up/Down move   Space toggle   Enter accept   q quit\n", last)

    def test_long_items_are_cut_to_the_width(self) -> None:
        term, out = make("\n", cols=30)
        term.menu(0, ["x" * 50])
        # O10 W1: still exactly the width, now keeping the beginning and the end.
        self.assertIn("   > " + "x" * 6 + "..." + "x" * 13 + "\n", out.getvalue())



class TextPromptTests(unittest.TestCase):
    """W3.1, W3.3: setup.sh's ask_text: a default, home markers expanded, q asks to quit."""

    def test_nothing_typed_takes_the_default_and_typed_text_is_expanded(self) -> None:
        term, out = make("\n")
        self.assertEqual(("/Users/<you>/projects", "/Users/<you>/projects"),
                         term.ask_text("Project path", "/Users/<you>/projects", home="/Users/<you>"))
        self.assertIn("  Project path [/Users/<you>/projects]\n  > ", out.getvalue())
        term, out = make(" '~/Code/app' \n")
        self.assertEqual(("/Users/<you>/Code/app", " '~/Code/app' "),
                         term.ask_text("Project path", "", home="/Users/<you>"))
        self.assertIn("  Project path (type or paste the full path)\n  > ", out.getvalue())

    def test_q_asks_to_quit_then_asks_again(self) -> None:
        term, out = make("q\n", "n", "/p\n")
        self.assertEqual(("/p", "/p"), term.ask_text("Project path", "", home="/h"))
        self.assertEqual(2, out.getvalue().count("Project path (type or paste the full path)"))
        term, _ = make("Q\n", "y")
        with self.assertRaises(terminal.Stop) as stopped:
            term.ask_text("Project path", "", home="/h")
        self.assertEqual(130, stopped.exception.code)

    def test_closed_input_stops_with_1(self) -> None:
        term, _ = make("/half")
        with self.assertRaises(terminal.Stop) as stopped:
            term.ask_text("Project path", "", home="/h")
        self.assertEqual((1, "Input closed; stopping."), (stopped.exception.code, stopped.exception.message))

    def test_tab_completes_paths_with_folders_ending_in_a_slash(self) -> None:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            base = os.path.realpath(directory)
            os.mkdir(os.path.join(base, "project"))
            open(os.path.join(base, "projects.txt"), "w").close()
            found = terminal.complete_path(os.path.join(base, "proj"), home="/h")
            self.assertEqual([os.path.join(base, "project") + "/", os.path.join(base, "projects.txt")], found)
            self.assertEqual([], terminal.complete_path(os.path.join(base, "zzz"), home="/h"))
            os.mkdir(os.path.join(base, "home"))
            os.mkdir(os.path.join(base, "home", "Code"))
            self.assertEqual(["~/Code/"], terminal.complete_path("~/Co", home=os.path.join(base, "home")))



class ViewTests(unittest.TestCase):
    """setup.sh's view_plan: a scrollable list, a page at a time, q, Enter or Esc to go back."""

    def test_the_view_pages_scrolls_clamps_and_returns(self) -> None:
        text = "\n".join(f"line {n}" for n in range(1, 31))
        term, out = make("j", "\033[6~", "\033[6~", "\033[6~", "k", "q")
        term.view_text(text, "Full plan")
        frames = out.getvalue().split("\033[H\033[2J")[1:]
        self.assertEqual(6, len(frames))
        self.assertIn("\n  Full plan: lines 1-15 of 30\n\n    line 1\n", frames[0])  # 24 rows: 15 a page.
        self.assertIn("Full plan: lines 2-16 of 30", frames[1])
        self.assertIn("Full plan: lines 16-30 of 30", frames[3])  # Clamped at the end.
        self.assertIn("Full plan: lines 15-29 of 30", frames[5])
        self.assertTrue(frames[0].endswith("\n  Up/Down scroll   PageUp/PageDown page   q back\n"))

    def test_closed_input_just_returns(self) -> None:
        term, _ = make()
        term.view_text("a", "Title")



class LineEditorTests(unittest.TestCase):
    """W3.3: the path prompt's own line editor (what read -e gave setup.sh's prompts): typing, Backspace,
    Ctrl-U, Ctrl-W, Ctrl-V for a literal character, Tab completion, Enter, and Ctrl-D on an empty line."""

    def edit(self, *pressed, complete=lambda text: []):
        term, out = make(*pressed)
        return term.edit_line(complete), out.getvalue()

    def test_typing_and_deleting(self) -> None:
        self.assertEqual("abc", self.edit("abc\n")[0])
        self.assertEqual("ac", self.edit("ab\x7fc\n")[0])
        self.assertEqual("ac", self.edit("ab\x08c\n")[0])
        self.assertEqual("new", self.edit("old path\x15new\n")[0])
        self.assertEqual("/a/b/c ", self.edit("/a/b/c d\x17\n")[0])  # Ctrl-W: back to the last space,
        self.assertEqual("", self.edit("/a/b/c d\x17\x17\n")[0])  # trailing spaces first (readline's rubout).
        self.assertEqual("x", self.edit("\x7fx\n")[0])  # Backspace on nothing does nothing.

    def test_the_line_is_echoed_and_erased_on_screen(self) -> None:
        line, shown = self.edit("ab\x7fc\n")
        self.assertEqual("ac", line)
        self.assertEqual("ab\b \bc\r\n", shown)

    def test_ctrl_v_inserts_the_next_character_literally(self) -> None:
        self.assertEqual("a\x01b", self.edit("a\x16\x01b\n")[0])
        self.assertEqual("a\nb", self.edit("a\x16\nb\n")[0])

    def test_tab_completes_a_single_match_or_the_common_start(self) -> None:
        def complete(text):
            return {"/p": ["/projects/", "/private/"], "/pro": ["/projects/"]}.get(text, [])
        self.assertEqual("/projects/app", self.edit("/pro\tapp\n", complete=complete)[0])
        self.assertEqual("/pr", self.edit("/p\t\n", complete=complete)[0])
        self.assertEqual("/zz", self.edit("/zz\t\n", complete=complete)[0])

    def test_escape_sequences_are_ignored(self) -> None:
        self.assertEqual("ab", self.edit("a\033[Db\n")[0])

    def test_end_of_input(self) -> None:
        self.assertIsNone(self.edit("\x04")[0])
        self.assertIsNone(self.edit("ab")[0])  # Input closed before Enter.
        self.assertEqual("ab", self.edit("ab\x04\n")[0])  # Ctrl-D in a line does nothing.



class CompletionSafetyTests(unittest.TestCase):
    """Review 040-s5-wizard (b): a file name with a control character is never offered by Tab."""

    def test_names_with_control_characters_are_not_completed(self) -> None:
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            base = os.path.realpath(directory)
            open(os.path.join(base, "evil\x1b[2Jname"), "w").close()
            open(os.path.join(base, "fine"), "w").close()
            self.assertEqual([os.path.join(base, "fine")], terminal.complete_path(os.path.join(base, ""), home="/h"))
            self.assertEqual([], terminal.complete_path(os.path.join(base, "ev"), home="/h"))



class DisplaySafetyTests(unittest.TestCase):
    """Review 040-s5-wizard-2 (d): nothing shown on screen carries a control character (C0, DEL or C1),
    whatever its source; colour comes only from the terminal layer itself."""

    def test_lists_bodies_menus_and_prompts_are_stripped_of_controls(self) -> None:
        term, out = make("\n", "\n")
        term.body("one\nline\x1b]0;title\x07 end")
        term.list_add("ok", "path\x1b[2J\x9b31m")
        term.list_sub("sub\x07")
        term.list_cmd("cmd\x1b")
        term.list_plain("plain\x7f")
        term.list_heading("Heading\x1b")
        term.menu(0, ["item\x1b[31m"])
        term.ask_text("Prompt\x1b", "/def\x1b[2Jault", home="/h")
        # The terminal layer's own codes and line endings, then nothing from the content may remain.
        text = (out.getvalue().replace("\033[H\033[2J", "").replace("\033[?25l", "").replace("\033[?25h", "")
                .replace("\r\n", "\n"))
        self.assertNotRegex(text, "[\x00-\x09\x0b-\x1f\x7f-\x9f]")
        for shown in ("one", "line]0;title end", "path[2J31m", "sub", "item[31m", "/def[2Jault", "Heading"):
            self.assertIn(shown, text)


if __name__ == "__main__":
    unittest.main()
