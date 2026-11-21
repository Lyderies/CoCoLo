"""CI-4: what Claude Code 2.1.323 reports in its init frame under COCOLO's real child settings.

The frame follows a capture with the real settings (`bridge.claude_settings`: hooks off, the known built-in
plugins disabled, `dontAsk`; `--tools ""`, `--setting-sources project`, `--strict-mcp-config`): the plugin list
holds only `cc-plugin-privacy`, whose only effect is a `tool.check` hook that can restrict a call. The check
accepts exactly that one built-in, with exactly the keys name, path and source. The four built-ins COCOLO
disables (agents-md, telemetry, plugin-authoring, easel) are absent when its settings applied; one that is
listed means the settings did not apply (`init_plugin_not_disabled`). Any other plugin is
`init_plugin_not_allowed`. Tools, MCP servers, key source, model and mode are held as before.
"""
import unittest

from tests.test_bridge import RATE_LIMIT, BridgeTestCase, init_frame, result, result_frame

from codex_agents import bridge

DISABLED_NAMES = ("cc-plugin-agents-md", "cc-plugin-telemetry", "cc-plugin-plugin-authoring", "cc-plugin-easel")
STRUCTURED = ["StructuredOutput", "Read", "Glob", "Grep"]


def builtin(name):
    return {"name": name, "path": "builtin", "source": f"{name}@builtin"}


PRIVACY = builtin("cc-plugin-privacy")


def frame_2_1_323(**changes):
    """The captured init shape under the real child settings: privacy alone, and the keys 2.1.323 added."""
    frame = {
        "model": "claude-haiku-5-5", "tools": [], "mcp_servers": [], "claude_code_version": "2.1.323",
        "plugins": [dict(PRIVACY)],
        "permissionMode": "dontAsk", "additional_directories": [], "agents": [], "skills": [],
        "slash_commands": [], "capabilities": [], "memory_paths": {}, "output_style": "default",
        "analytics_disabled": True, "product_feedback_disabled": True, "fast_mode_disabled_reason": None,
        "fast_mode_state": "off", "per_turn_effort_active": False, "ultrathink_available": False,
        "ultrathink_typed_raises": False, "view_mode": "default", "original_cwd": "/tmp/empty",
        "messaging_socket_path": "/tmp/x.sock", "terminal_slash_commands": [],
    }
    return init_frame(**{**frame, **changes})


class InitCheckTests(unittest.TestCase):
    def check(self, frame, allowed=bridge.CLAUDE_TOOLS, **keywords):
        return bridge._check_claude_init(frame, "claude-haiku-5-5", allowed, **keywords)

    def test_CI_1_the_captured_real_settings_frame_passes(self):
        self.assertIsNone(self.check(frame_2_1_323()))
        self.assertIsNone(self.check(frame_2_1_323(tools=STRUCTURED), bridge.CLAUDE_RUN_TOOLS, schema_held=True))
        self.assertIsNone(self.check(frame_2_1_323(plugins=[])))

    def test_CI_1_only_privacy_is_accepted(self):
        self.assertEqual({"cc-plugin-privacy"}, set(bridge.CLAUDE_ALLOWED_BUILTIN_PLUGINS))

    def test_CI_1_a_built_in_COCOLO_disables_means_its_settings_did_not_apply(self):
        for name in DISABLED_NAMES:
            with self.subTest(name=name):
                self.assertEqual("plugin_not_disabled", self.check(frame_2_1_323(plugins=[builtin(name)])))
                self.assertEqual("plugin_not_disabled", self.check(frame_2_1_323(plugins=[PRIVACY, builtin(name)])))
        four = [builtin(n) for n in ("cc-plugin-agents-md", "cc-plugin-telemetry", "cc-plugin-privacy",
                                     "cc-plugin-plugin-authoring")]
        self.assertEqual("plugin_not_disabled", self.check(frame_2_1_323(plugins=four)))

    def test_CI_1_every_name_COCOLO_disables_is_one_the_check_names(self):
        self.assertEqual({n + "@builtin" for n in DISABLED_NAMES}, set(bridge.CLAUDE_DISABLED_PLUGINS))

    def test_CI_1_other_plugins_still_stop_the_child(self):
        cases = {
            "another built-in": [builtin("cc-plugin-sec-default")],
            "unknown built-in with privacy": [PRIVACY, builtin("cc-plugin-mermaid")],
            "user plugin": [{"name": "mine", "path": "/plugins/mine", "source": "mine@user"}],
            "marketplace plugin": [{"name": "cc-plugin-privacy", "path": "/m/p", "source": "cc-plugin-privacy@market"}],
            "privacy, other path": [{**PRIVACY, "path": "/tmp/p"}],
            "privacy, other source": [{**PRIVACY, "source": "cc-plugin-privacy@user"}],
            "privacy, source of another": [{**PRIVACY, "source": "cc-plugin-telemetry@builtin"}],
            "disabled name, other path": [{**builtin("cc-plugin-telemetry"), "path": "/tmp/t"}],
            "disabled name, other source": [{**builtin("cc-plugin-telemetry"), "source": "cc-plugin-telemetry@user"}],
            "name only": [{"name": "cc-plugin-privacy"}],
            "name and path only": [{"name": "cc-plugin-privacy", "path": "builtin"}],
            "name and source only": [{"name": "cc-plugin-privacy", "source": "cc-plugin-privacy@builtin"}],
            "extra field hooks": [{**PRIVACY, "hooks": ["tool.check"]}],
            "extra field enabled": [{**PRIVACY, "enabled": True}],
            "extra field of any name": [{**PRIVACY, "x": None}],
            "not an object": ["cc-plugin-privacy"],
            "empty name": [{"name": "", "path": "builtin", "source": "@builtin"}],
            "upper case": [builtin("CC-PLUGIN-PRIVACY")],
            "trailing space": [builtin("cc-plugin-privacy ")],
            "leading space": [builtin(" cc-plugin-privacy")],
            "near name": [builtin("cc-plugin-privacy2")],
            "prefix of the name": [builtin("cc-plugin-priv")],
            "underscore for hyphen": [builtin("cc_plugin_privacy")],
            "non-string name": [{"name": ["cc-plugin-privacy"], "path": "builtin", "source": "x@builtin"}],
            "non-string path": [{**PRIVACY, "path": ["builtin"]}],
            "name with @builtin": [builtin("cc-plugin-privacy@builtin")],
        }
        for label, plugins in cases.items():
            with self.subTest(label=label):
                self.assertEqual("plugin_not_allowed", self.check(frame_2_1_323(plugins=plugins)))
        for plugins in (None, "none", {}, 0):
            with self.subTest(plugins=plugins):
                self.assertEqual("plugin_not_allowed", self.check(frame_2_1_323(plugins=plugins)))
        absent = frame_2_1_323()
        del absent["plugins"]
        self.assertEqual("plugin_not_allowed", self.check(absent))

    def test_CI_1_a_plugin_that_is_not_allowed_outranks_one_that_is_not_disabled(self):
        both = [builtin("cc-plugin-telemetry"), {"name": "mine", "path": "/p", "source": "mine@user"}]
        self.assertEqual("plugin_not_allowed", self.check(frame_2_1_323(plugins=both)))

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
    """CI-1 and CI-3 for a routed child: the real-settings frame runs to its result, a disallowed one stops."""

    def run_with(self, first):
        route, assigned, handle = self.claude_run()
        self.frames("claude", [first, {"sleep": 30}, result_frame()])
        return self.dispatch(route, assigned, handle)["bridge_report"]["outcome"]

    def test_CI_1_a_child_with_the_real_settings_frame_runs_to_its_result(self):
        route, assigned, handle = self.claude_run()
        init = frame_2_1_323(model="claude-opus-5-5", tools=STRUCTURED)
        self.frames("claude", [init, RATE_LIMIT, result_frame()])
        output = self.dispatch(route, assigned, handle)
        report = output["bridge_report"]
        self.assertEqual(("result", None), (report["outcome"], report["terminal_state"]))
        self.assertEqual(result(), output["result"])

    def test_CI_3_a_child_stops_with_the_reason_of_the_violation(self):
        model = "claude-opus-5-5"
        cases = {
            "init_plugin_not_allowed": frame_2_1_323(model=model, tools=STRUCTURED,
                                                      plugins=[builtin("cc-plugin-sec-default")]),
            "init_plugin_not_disabled": frame_2_1_323(model=model, tools=STRUCTURED,
                                                       plugins=[PRIVACY, builtin("cc-plugin-telemetry")]),
            "init_permission_mode_mismatch": frame_2_1_323(model=model, tools=STRUCTURED, permissionMode="default"),
            "init_additional_directories_present": frame_2_1_323(model=model, tools=STRUCTURED,
                                                                  additional_directories=["/x"]),
        }
        for reason, init in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(reason, self.run_with(init))


if __name__ == "__main__":
    unittest.main()
