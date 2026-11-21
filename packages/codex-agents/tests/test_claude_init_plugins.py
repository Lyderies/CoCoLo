"""CI-4: Claude Code 2.1.323 reports four built-in plugins in its init frame; COCOLO accepts exactly those.

The frame follows a real `claude -p --model claude-haiku-5-5 --tools "" --setting-sources project
--strict-mcp-config` capture (Claude Code 2.1.323, 2026-11-21). The check stays closed: any other plugin, a
built-in name with a non-built-in path or source, a tool, an MCP server, another model or key source still
stops the child, each with its own fixed reason, and a probe reports that reason instead of probe_incomplete.
"""
import unittest

from tests.test_bridge import RATE_LIMIT, BridgeTestCase, init_frame, result_frame

from codex_agents import bridge

BUILTIN_NAMES = ("cc-plugin-agents-md", "cc-plugin-telemetry", "cc-plugin-privacy", "cc-plugin-plugin-authoring")
STRUCTURED = ["StructuredOutput", "Read", "Glob", "Grep"]


def builtin(name):
    return {"name": name, "path": "builtin", "source": f"{name}@builtin"}


def frame_2_1_323(**changes):
    """The captured init shape: four built-in plugins and the keys 2.1.323 added."""
    return init_frame(
        model="claude-haiku-5-5", tools=[], mcp_servers=[], claude_code_version="2.1.323",
        plugins=[builtin(name) for name in BUILTIN_NAMES],
        permissionMode="dontAsk", additional_directories=[], agents=[], skills=[], slash_commands=[],
        capabilities=[], memory_paths={}, output_style="default", analytics_disabled=True,
        product_feedback_disabled=True, fast_mode_disabled_reason=None, fast_mode_state="off",
        per_turn_effort_active=False, ultrathink_available=False, ultrathink_typed_raises=False,
        view_mode="default", original_cwd="/tmp/empty", messaging_socket_path="/tmp/x.sock",
        terminal_slash_commands=[], **changes)


class InitCheckTests(unittest.TestCase):
    def check(self, frame, allowed=bridge.CLAUDE_TOOLS, **keywords):
        return bridge._check_claude_init(frame, "claude-haiku-5-5", allowed, **keywords)

    def test_CI_1_the_captured_2_1_323_frame_passes(self):
        self.assertIsNone(self.check(frame_2_1_323()))
        self.assertIsNone(self.check(frame_2_1_323(tools=STRUCTURED), bridge.CLAUDE_RUN_TOOLS, schema_held=True))

    def test_CI_1_each_built_in_alone_and_none_pass(self):
        for name in BUILTIN_NAMES:
            with self.subTest(name=name):
                self.assertIsNone(self.check(frame_2_1_323(plugins=[builtin(name)])))
        self.assertIsNone(self.check(frame_2_1_323(plugins=[])))

    def test_CI_1_other_plugins_still_stop_the_child(self):
        cases = {
            "unknown built-in": [*[builtin(n) for n in BUILTIN_NAMES], builtin("cc-plugin-easel")],
            "another known built-in": [builtin("cc-plugin-sec-default")],
            "user plugin": [{"name": "mine", "path": "/plugins/mine", "source": "mine@user"}],
            "marketplace plugin": [{"name": "cc-plugin-privacy", "path": "/m/p", "source": "cc-plugin-privacy@market"}],
            "built-in name, other path": [{"name": "cc-plugin-privacy", "path": "/tmp/p", "source": "cc-plugin-privacy@builtin"}],
            "built-in name, other source": [{"name": "cc-plugin-privacy", "path": "builtin", "source": "cc-plugin-privacy@user"}],
            "built-in name, source of another": [{"name": "cc-plugin-privacy", "path": "builtin", "source": "cc-plugin-telemetry@builtin"}],
            "name only": [{"name": "cc-plugin-privacy"}],
            "not an object": ["cc-plugin-privacy"],
            "empty name": [{"name": "", "path": "builtin", "source": "@builtin"}],
        }
        for label, plugins in cases.items():
            with self.subTest(label=label):
                self.assertEqual("plugin_not_allowed", self.check(frame_2_1_323(plugins=plugins)))
        for plugins in (None, "none", {}):
            with self.subTest(plugins=plugins):
                self.assertEqual("plugin_not_allowed", self.check(frame_2_1_323(plugins=plugins)))
        absent = frame_2_1_323()
        del absent["plugins"]
        self.assertEqual("plugin_not_allowed", self.check(absent))

    def test_CI_1_the_other_rules_are_unchanged(self):
        self.assertEqual("api_key_source", self.check(frame_2_1_323(apiKeySource="ANTHROPIC_API_KEY")))
        self.assertEqual("model_mismatch", self.check(frame_2_1_323(model="claude-sonnet-5-5")))
        self.assertEqual("mcp_server_loaded", self.check(frame_2_1_323(mcp_servers=[{"name": "x"}])))
        self.assertEqual("tool_not_allowed", self.check(frame_2_1_323(tools=["Bash"])))
        self.assertEqual("tool_not_allowed", self.check(frame_2_1_323(tools=[7])))

    def test_CI_2_a_permission_mode_other_than_the_one_set_stops_the_child(self):
        for mode in ("default", "bypassPermissions", "acceptEdits", "plan", None, ""):
            with self.subTest(mode=mode):
                self.assertEqual("permission_mode_mismatch", self.check(frame_2_1_323(permissionMode=mode)))

    def test_CI_2_an_added_directory_stops_the_child(self):
        for value in (["/extra"], "/extra", None):
            with self.subTest(value=value):
                self.assertEqual("additional_directories_present",
                                 self.check(frame_2_1_323(additional_directories=value)))

    def test_CI_2_an_older_client_without_those_keys_still_passes(self):
        frame = init_frame(model="claude-haiku-5-5", tools=[], claude_code_version="2.1.311")
        self.assertNotIn("permissionMode", frame)
        self.assertIsNone(self.check(frame))

    def test_CI_4_only_a_plain_version_is_recorded(self):
        self.assertEqual("2.1.323", bridge._init_client_version(frame_2_1_323()))
        for version in (None, "unknown", 7, "2.1.323; rm -rf /", "2.1.323 (Claude Code)", "", "2.1", ["2.1.323"]):
            with self.subTest(version=version):
                self.assertIsNone(bridge._init_client_version(frame_2_1_323(claude_code_version=version)))
        self.assertIsNone(bridge._init_client_version({}))


class ChildInitTests(BridgeTestCase):
    """CI-1 and CI-3 for a routed child: the 2.1.323 frame runs, a disallowed one stops with its reason."""

    def run_with(self, first):
        route, assigned, handle = self.claude_run()
        self.frames("claude", [first, {"sleep": 30}, result_frame()])
        return self.dispatch(route, assigned, handle)["bridge_report"]["outcome"]

    def test_CI_1_a_child_with_the_four_built_ins_is_not_stopped_at_init(self):
        route, assigned, handle = self.claude_run()
        self.frames("claude", [frame_2_1_323(model="claude-opus-5-5", tools=STRUCTURED), RATE_LIMIT, result_frame()])
        self.assertNotEqual("init_plugin_not_allowed", self.dispatch(route, assigned, handle)["bridge_report"]["outcome"])

    def test_CI_3_a_child_with_another_plugin_stops_with_its_reason(self):
        init = frame_2_1_323(model="claude-opus-5-5", tools=STRUCTURED, plugins=[builtin("cc-plugin-easel")])
        self.assertEqual("init_plugin_not_allowed", self.run_with(init))


if __name__ == "__main__":
    unittest.main()
