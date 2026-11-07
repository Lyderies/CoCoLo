from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import contextlib
import functools
import hashlib
import base64
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents import host_paths, providers
from codex_agents.contracts import ContractError
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_contracts import (
    canonical_json,
    content_digest,
    keyed_digest,
    validate_learning_record,
)
from codex_agents.routing import load_policy
from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.learning_store import (
    PRODUCER_AUTHORITY,
    LearningLedger,
    LearningState,
    LedgerError,
    OWNER_SIGNER_LINE,
    StateError,
    _owner_key_identity,
    _ssh_key_parts,
    authorize_collector_runtime,
)
from codex_agents.providers import (
    APP_SERVER_METHODS,
    APP_SERVER_NOTIFICATIONS,
    IDENTITY_MAX_AGE_MS,
    AdmissionDenied,
    AppServerSession,
    billing_attestation_record,
    claude_rate_limit_records,
    codex_exec_options,
    evaluate_admission,
    evaluate_launch,
    observe_claude,
    parse_client_version,
    window_start_ms,
    observe_codex,
    parse_claude_auth_status,
    parse_codex_rate_limits,
    record_claude_rate_limit_event,
)
from tests.learning_support import (
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    assignment,
    event_record,
    attest_provider,
    fake_provider_tools,
    generate_test_owner_key,
    provider_fixture,
    trusted_tools,
    routing_fields,
    verification_outcome,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "providers"
CODEX_LIMITS = {
    "admission_policy": {
        "client_versions": {"minimum": "0.164.0", "maximum_exclusive": "0.164.1"},
        "max_admissions_per_window": 60,
    },
    "prior_run_admissions": [],
    "window_headroom": [],
}
CLIENT = ("/usr/bin/fake-client", "f" * 64)
parse_claude = functools.partial(parse_claude_auth_status, client=CLIENT)
parse_codex = functools.partial(parse_codex_rate_limits, client=CLIENT)
KEY = b"k" * 32
NOW = 10_000_000_000
CLEAN: dict[str, str] = {}
RAW_IDENTITY = (
    b"fixture-owner@example.invalid",
    b"fixture-org-7f3a91",
    b"Fixture Organisation Name",
    b"fixture-account-5c2e08",
)

# A stand-in for `codex app-server`: it logs each method it receives and
# answers from the recorded fixture. It never contacts a provider.
FAKE_APP_SERVER = """\
import json, sys
log = open(sys.argv[1], "a")
fixture = json.load(open(sys.argv[2]))
print(json.dumps({"method": "account/rateLimits/updated", "params": {}}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    log.write(message["method"] + "\\n")
    log.flush()
    if "id" not in message:
        continue
    if message["method"] == "initialize":
        result = {"userAgent": "fake"}
    elif message["method"] == "account/rateLimits/read":
        result = fixture
    else:
        print(json.dumps({"id": message["id"], "error": {"code": -1}}), flush=True)
        continue
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
"""


def fake_codex(directory: Path) -> tuple[dict[str, str], Path]:
    log = directory / "methods.log"
    script = directory / "fake_app_server.py"
    script.write_text(FAKE_APP_SERVER)
    executable = directory / "codex"
    executable.write_text(
        f"#!/bin/sh\nexec {sys.executable} {script} {log} "
        f"{Path(__file__).resolve().parent / 'fixtures/providers/codex-rate-limits.json'}\n"
    )
    executable.chmod(0o755)
    return {"PATH": str(directory)}, log


class ProviderObservationTests(unittest.TestCase):
    def test_claude_auth_status_becomes_a_fingerprint(self) -> None:
        identity = parse_claude(
            provider_fixture("claude-auth-status.json"),
            digest_key=KEY,
            observed_at_ms=NOW,
        )
        self.assertEqual(
            ("claude", True, "claude.ai", "max"),
            (
                identity["provider"], identity["logged_in"],
                identity["auth_method"], identity["subscription_type"],
            ),
        )
        self.assertEqual(
            keyed_digest(
                KEY, "claude\x00fixture-org-7f3a91\x00fixture-owner@example.invalid"
            ),
            identity["account_fingerprint"],
        )
        serialized = canonical_json(identity)
        for raw in RAW_IDENTITY:
            self.assertNotIn(raw, serialized)
        with self.assertRaisesRegex(ContractError, "identity is incomplete"):
            parse_claude(
                b'{"loggedIn": false}', digest_key=KEY, observed_at_ms=NOW
            )

    def test_codex_rate_limits_identify_windows_by_duration(self) -> None:
        identity, headroom, billing = parse_codex(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=KEY,
            observed_at_ms=NOW,
        )
        self.assertEqual("plus", identity["plan_type"])
        self.assertEqual(
            keyed_digest(KEY, "codex\x00fixture-account-5c2e08"),
            identity["account_fingerprint"],
        )
        self.assertEqual(
            [(300, 12.0, 1_793_450_000_000), (10_080, 42.0, 1_793_500_000_000)],
            [
                (window["window_minutes"], window["used_percent"], window["resets_at"])
                for window in headroom["windows"]
            ],
        )
        self.assertEqual(
            (False, False, False, True),
            (
                billing["paid_usage_usable"], billing["has_credits"],
                billing["unlimited"], billing["ordinary_usage_allowed"],
            ),
        )
        for record in (identity, headroom, billing):
            for raw in RAW_IDENTITY:
                self.assertNotIn(raw, canonical_json(record))

    def test_claude_rate_limit_event_records_headroom_and_billing(self) -> None:
        headroom, billing = claude_rate_limit_records(
            json.loads(provider_fixture("claude-rate-limit-event.json")),
            account_fingerprint=DIGEST_A,
            observed_at_ms=NOW,
        )
        self.assertEqual("allowed", headroom["status"])
        self.assertEqual(
            [{"window_minutes": 300, "scope": "five_hour",
              "used_percent": 25.0, "resets_at": 1_793_450_000_000}],
            headroom["windows"],
        )
        self.assertEqual(
            (False, False, "rejected"),
            (
                billing["paid_usage_usable"], billing["is_using_overage"],
                billing["overage_status"],
            ),
        )
        unified, _ = claude_rate_limit_records(
            {"status": "allowed", "unifiedWindows": {
                "five_hour": {"utilization": 0.5, "status": "allowed"},
                "seven_day": {"utilization": 0.9, "status": "rejected"},
            }},
            account_fingerprint=DIGEST_A,
            observed_at_ms=NOW,
        )
        self.assertEqual("rejected", unified["status"])
        self.assertEqual(
            [300, 10_080], [window["window_minutes"] for window in unified["windows"]]
        )
        with self.assertRaisesRegex(ContractError, "fraction"):
            claude_rate_limit_records(
                {"status": "allowed", "utilization": 25},
                account_fingerprint=DIGEST_A, observed_at_ms=NOW,
            )

    def test_observe_claude_uses_fixed_arguments_without_api_keys(self) -> None:
        calls = []

        def popen(arguments, **options):
            calls.append((arguments, options))
            return subprocess.Popen(
                ["/bin/cat", str(FIXTURES / "claude-auth-status.json")], **options
            )

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            executable = Path(directory) / "claude"
            executable.write_text("#!/bin/sh\nexit 1\n")
            executable.chmod(0o755)
            environ = {"CLAUDE_CONFIG_DIR": "/elsewhere", "CODEX_HOME": "/elsewhere"}
            with trusted_tools(Path(directory)):
                records = observe_claude(
                    tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW,
                    environ=environ, popen=popen,
                )
                self.assertEqual("provider_identity", records[0]["record_type"])
                real = os.path.realpath(executable)
                self.assertEqual([real, "auth", "status", "--json"], calls[0][0])
                # The version is read from the same resolved client, the same way.
                self.assertEqual([real, "--version"], calls[1][0])
                self.assertEqual(calls[0][1]["env"], calls[1][1]["env"])
                # This stand-in prints the auth JSON for every command, which
                # is not a version, so none is recorded.
                self.assertIsNone(records[0]["client_version"])
                self.assertTrue(calls[0][1]["start_new_session"])
                # The child gets only a minimal environment; overrides are dropped.
                self.assertEqual(
                    {"HOME", "USER", "LOGNAME", "PATH", "LANG", "TMPDIR", "xcrun_nocache"},
                    set(calls[0][1]["env"]),
                )
                # Any xcrun shim the client reaches skips its $TMPDIR lookup cache.
                self.assertEqual("1", calls[0][1]["env"]["xcrun_nocache"])
                # TMPDIR is the caller's private directory, never the system one.
                self.assertEqual(directory, calls[0][1]["env"]["TMPDIR"])
                for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                    with self.subTest(name=name), self.assertRaisesRegex(
                        ContractError, "API key"
                    ):
                        observe_claude(
                            tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW,
                            environ={**environ, name: "set"}, popen=popen,
                        )
            self.assertEqual(2, len(calls))

    def test_codex_exec_options_are_read_from_the_clients_help(self) -> None:
        sample = (
            "Options:\n  -c, --config <key=value>\n          Examples: - `-c model=\"o3\"`\n"
            "      --ignore-user-config\n          Do not load `$CODEX_HOME/config.toml`\n"
            "      --ignore-rules\n          Do not load user or project execpolicy files\n"
        )
        calls = []

        def popen(arguments, **options):
            calls.append((arguments, options))
            return subprocess.Popen(
                [sys.executable, "-c", f"import sys; sys.stdout.write({sample!r})"], **options
            )

        def failing(arguments, **options):
            return subprocess.Popen(["/usr/bin/false"], **options)

        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            options = codex_exec_options(
                "/fake/codex", environ={"CODEX_HOME": "/elsewhere"},
                tmpdir=Path(directory), popen=popen,
            )
            self.assertEqual({"--config", "--ignore-user-config", "--ignore-rules"}, options)
            self.assertEqual(["/fake/codex", "exec", "--help"], calls[0][0])
            self.assertNotIn("CODEX_HOME", calls[0][1]["env"])
            self.assertTrue(calls[0][1]["start_new_session"])
            # A failed help run lists nothing, so the bridge refuses the launch.
            self.assertEqual(frozenset(), codex_exec_options(
                "/fake/codex", environ={}, tmpdir=Path(directory), popen=failing,
            ))

    def test_clients_resolve_only_from_the_trusted_path(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            trusted = root / "trusted"
            planted = root / "planted"
            fake_provider_tools(trusted)
            marker = root / "planted-ran"
            planted.mkdir()
            impostor = planted / "claude"
            impostor.write_text(f"#!/bin/sh\n/usr/bin/touch {marker}\nexit 1\n")
            impostor.chmod(0o755)
            with trusted_tools(trusted):
                records = observe_claude(
                    tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW,
                    environ={"PATH": f"{planted}{os.pathsep}{trusted}"},
                )
            self.assertFalse(marker.exists())
            real = os.path.realpath(trusted / "claude")
            self.assertEqual(real, records[0]["client_path"])
            self.assertEqual(
                hashlib.sha256(Path(real).read_bytes()).hexdigest(),
                records[0]["client_sha256"],
            )
            with trusted_tools(planted / "missing"), self.assertRaisesRegex(
                ContractError, "trusted path"
            ):
                observe_claude(
                    tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW,
                    environ={"PATH": str(trusted)},
                )

    def test_clients_resolving_into_writable_locations_never_run(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            search = root / "bin"
            search.mkdir()
            marker = root / "client-ran"
            checkout = root / "project" / "tools"
            checkout.mkdir(parents=True)
            target = checkout / "claude"
            target.write_text(f"#!/bin/sh\n/usr/bin/touch {marker}\nexit 1\n")
            target.chmod(0o755)
            # The search path itself is trusted; its entry links into a checkout.
            (search / "claude").symlink_to(target)
            with mock.patch.object(
                providers, "TRUSTED_SEARCH_DIRECTORIES", (str(search),)
            ):
                # The real temporary roots hold the target.
                with self.assertRaisesRegex(ContractError, "trusted location") as caught:
                    observe_claude(
                        tmpdir=root, digest_key=KEY, observed_at_ms=NOW, environ={},
                    )
                self.assertEqual("client_unavailable", caught.exception.reason_code)
                # Outside any temporary root, the project still refuses it, and so
                # does a folder that is no trusted installation folder (a sibling
                # of a workspace, such as ~/Work/tools or /srv/work/tools).
                with mock.patch.object(providers, "temp_roots", lambda: set()):
                    with self.assertRaisesRegex(ContractError, "trusted location"):
                        providers.resolve_client("claude", untrusted=[root / "project"])
                    with self.assertRaisesRegex(ContractError, "trusted location"):
                        providers.resolve_client("claude", untrusted=[])
                    with mock.patch.object(
                        host_paths, "TRUSTED_INSTALL_ROOTS",
                        (*host_paths.TRUSTED_INSTALL_ROOTS, os.path.realpath(checkout)),
                    ):
                        self.assertEqual(
                            os.path.realpath(target),
                            providers.resolve_client("claude", untrusted=[])[0],
                        )
            self.assertFalse(marker.exists())

    def test_claude_output_is_bounded_while_streaming(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            marker = root / "still-running"
            # Each stand-in records its process and keeps running unless killed.
            scripts = {
                "flood": "import sys\nwhile True: sys.stdout.write('x' * 65536)\n",
                "stall": "import time\ntime.sleep(60)\n",
            }
            for name, body in scripts.items():
                script = root / f"{name}.py"
                script.write_text(
                    f"import os\nopen({str(marker)!r} + '-{name}', 'w')"
                    f".write(str(os.getpid()))\n{body}"
                )
                executable = root / name / "claude"
                executable.parent.mkdir()
                executable.write_text(f"#!/bin/sh\nexec {sys.executable} {script}\n")
                executable.chmod(0o755)
            for name, message in (("flood", "too large"), ("stall", "timed out")):
                with self.subTest(name=name):
                    started = time.monotonic()
                    with trusted_tools(root / name), self.assertRaisesRegex(
                        ContractError, message
                    ):
                        observe_claude(
                            tmpdir=Path(directory), digest_key=KEY, observed_at_ms=NOW, environ={}, timeout=1,
                        )
                    self.assertLess(time.monotonic() - started, 10)
                    pid = int(Path(f"{marker}-{name}").read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_labels_are_allow_listed_and_identifiers_never_leak(self) -> None:
        status = json.loads(provider_fixture("claude-auth-status.json"))
        status.update(subscriptionType="fixture-org-7f3a91", authMethod="fixture-org-7f3a91")
        identity = parse_claude(
            json.dumps(status), digest_key=KEY, observed_at_ms=NOW
        )
        self.assertEqual((None, None), (identity["subscription_type"], identity["auth_method"]))
        self.assertNotIn(b"fixture-org-7f3a91", canonical_json(identity))
        result = json.loads(provider_fixture("codex-rate-limits.json"))
        result["rateLimits"]["planType"] = "fixture-account-5c2e08"
        records = parse_codex(result, digest_key=KEY, observed_at_ms=NOW)
        self.assertIsNone(records[0]["plan_type"])
        for record in records:
            self.assertNotIn(b"fixture-account-5c2e08", canonical_json(record))
        result["rateLimits"]["planType"] = "prolite"
        self.assertEqual(
            "prolite",
            parse_codex(result, digest_key=KEY, observed_at_ms=NOW)[0][
                "plan_type"
            ],
        )

    def test_claude_billing_is_unusable_only_on_exact_evidence(self) -> None:
        def usable(**info) -> object:
            return claude_rate_limit_records(
                {"status": "allowed", **info},
                account_fingerprint=DIGEST_A, observed_at_ms=NOW,
            )[1]["paid_usage_usable"]

        rejected = {
            "isUsingOverage": False, "overageStatus": "rejected",
            "overageDisabledReason": "org_level_disabled",
        }
        self.assertIs(False, usable(**rejected))
        for change in (
            {"overageDisabledReason": "anything at all"},
            {"overageDisabledReason": "fetch_error"},
            {"overageDisabledReason": None},
            {"overageStatus": None, "overageDisabledReason": "org_level_disabled"},
            {"overageStatus": "disabled"},
            {"isUsingOverage": None},
            {"isUsingOverage": "false"},
        ):
            with self.subTest(change=change):
                self.assertIsNone(usable(**{**rejected, **change}))
        self.assertIs(True, usable(**{**rejected, "isUsingOverage": True}))
        self.assertIs(True, usable(**{**rejected, "overageStatus": "allowed"}))

    def test_app_server_sends_only_allow_listed_methods(self) -> None:
        self.assertEqual(("initialize", "account/rateLimits/read"), APP_SERVER_METHODS)
        self.assertEqual(("initialized",), APP_SERVER_NOTIFICATIONS)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            _, log = fake_codex(root)
            environ: dict[str, str] = {}
            with trusted_tools(root):
                records = observe_codex(
                    root, digest_key=KEY, observed_at_ms=NOW, environ=environ,
                    tmpdir=Path(directory),
                )
            self.assertEqual(
                ["provider_identity", "provider_headroom", "provider_billing"],
                [record["record_type"] for record in records],
            )
            self.assertEqual(
                ["initialize", "initialized", "account/rateLimits/read"], log.read_text().split()
            )
            with trusted_tools(root):
                session = AppServerSession(root, environ=environ, tmpdir=root)
            try:
                for method in ("account/logout", "thread/start", "turn/start"):
                    with self.subTest(method=method), self.assertRaisesRegex(
                        ContractError, "not allow-listed"
                    ):
                        session.request(method, {})
                for method in ("account/logout", "initialize"):
                    with self.subTest(notification=method), self.assertRaisesRegex(
                        ContractError, "not allow-listed"
                    ):
                        session.notify(method)
            finally:
                session.close()
            self.assertEqual(
                ["initialize", "initialized", "account/rateLimits/read"], log.read_text().split()
            )
            with trusted_tools(root), self.assertRaisesRegex(ContractError, "API key"):
                AppServerSession(root, environ={**environ, "OPENAI_API_KEY": "x"}, tmpdir=root)


class HeadroomThresholdTests(unittest.TestCase):
    def test_claude_windows_use_the_codex_thresholds(self) -> None:
        from codex_agents.providers import _headroom_state

        def rate(used: float | None, status: str = "allowed") -> str | None:
            record = {
                "status": status, "observed_at_ms": NOW,
                "windows": [{"window_minutes": 300, "scope": "five_hour",
                             "used_percent": used, "resets_at": NOW + 1000}],
            }
            return _headroom_state("claude", record, NOW)

        for used, expected in (
            (74.9, "ample"), (75, "constrained"), (89.9, "constrained"),
            (90, "exhausted"), (100, "exhausted"), (None, "ample"),
        ):
            with self.subTest(used=used):
                self.assertEqual(expected, rate(used))
        self.assertEqual("exhausted", rate(10, "rejected"))
        self.assertEqual("constrained", rate(10, "allowed_warning"))
        record = {
            "status": "rejected", "observed_at_ms": NOW,
            "windows": [{"window_minutes": 300, "scope": "five_hour",
                         "used_percent": 95, "resets_at": NOW - 1}],
        }
        self.assertIsNone(_headroom_state("claude", record, NOW))


class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        base = Path(self.temporary.name)
        self.project = base / "project"
        self.project.mkdir()
        self.tools = base / "fake-tools"
        self.environ = fake_provider_tools(self.tools)
        self.state = LearningState(base / "state")
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / "cli.py")
        self.owner_private_key, public_key = generate_test_owner_key(base)
        self.state.install_owner_public_key(public_key)
        self.clients = load_client_capabilities(
            ORCHESTRATION_ROOT / "config" / "learning-clients.json"
        )
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.project_id = self.ledger.register_project(self.project, now_ms=1)
        self.clock = NOW

    def tearDown(self) -> None:
        self.ledger.close()
        self.temporary.cleanup()

    def tick(self) -> int:
        # Each observation gets its own time, as successive real reads would.
        self.clock += 1
        return self.clock

    def admit(self, provider: str = "codex", **options) -> dict:
        return self.ledger.evaluate_admission(
            self.project_id, provider=provider,
            environ=options.pop("environ", CLEAN),
            now_ms=options.pop("now_ms", self.clock + 1),
        )

    def assert_denied(self, reason: str, provider: str = "codex", **options) -> None:
        with self.assertRaises(AdmissionDenied) as raised:
            self.admit(provider, **options)
        self.assertEqual(reason, raised.exception.reason_code)
        with self.ledger.repository(self.project_id) as repository:
            stored = repository.records("admission_decision")[-1]
        self.assertEqual(("deny", reason), (stored["decision"], stored["reason_code"]))

    def observe(self, provider: str = "codex", *, now_ms: int | None = None, **changes):
        """Run the ledger's own observation against the fake client's output."""

        if provider == "codex":
            result = json.loads(provider_fixture("codex-rate-limits.json"))
            for path, value in changes.items():
                target = result
                *parents, leaf = path.split(".")
                for parent in parents:
                    target = target[parent]
                target[leaf] = value
            (self.tools / "codex-result.json").write_text(json.dumps(result))
        else:
            status = {**json.loads(provider_fixture("claude-auth-status.json")), **changes}
            (self.tools / "claude-status.json").write_text(json.dumps(status))
        stamp = now_ms if now_ms is not None else self.tick()
        with trusted_tools(self.tools), mock.patch.object(
            self.ledger, "_clock_ms", return_value=stamp
        ):
            return self.ledger.observe_provider(
                self.project_id, provider, project_root=self.tools, environ=CLEAN
            )

    def event(self, run: dict, **info) -> list[dict]:
        """Record a run's rate-limit event; the ledger's clock stamps it."""

        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.tick()):
            return record_claude_rate_limit_event(
                self.ledger, self.project_id,
                run_handle=run["run_handle"], rate_limit_info=info,
            )

    def clean_claude_billing(self, at_ms: int) -> None:
        """Fresh, clean Claude billing at a given time, as a probe would record.

        Claude admission needs billing from the last 30 minutes, so tests that
        jump past a reset record it again at the new time.
        """

        identity = self.observe("claude", now_ms=at_ms)[0]
        _, billing = claude_rate_limit_records(
            {"status": "allowed", "isUsingOverage": False, "overageStatus": "rejected",
             "overageDisabledReason": "org_level_disabled"},
            account_fingerprint=identity["account_fingerprint"], observed_at_ms=at_ms,
        )
        self.ledger._store_provider_observations(self.project_id, [billing], timestamp=at_ms)

    def attest(self, provider: str, fingerprint: str) -> None:
        attest_provider(
            self.ledger, self.project_id, self.owner_private_key, provider, fingerprint
        )

    def open_run(self, provider: str = "codex", *, now_ms: int | None = None) -> dict:
        orchestration = self.ledger.open_orchestration(
            self.project_id, client_version="codex-host-v1", now_ms=NOW
        )
        stamp = now_ms if now_ms is not None else self.clock + 1
        with mock.patch.object(self.ledger, "_clock_ms", return_value=stamp):
            return self._open_run(provider, orchestration, stamp)

    def _open_run(self, provider: str, orchestration: dict, stamp: int) -> dict:
        assigned = assignment()
        configuration = {
            "model": assigned["model"], "effort": assigned["effort"],
            "role": assigned["role"], "topology": "single", "max_parallel": 1,
            "verification_required": True, "verifier_digest": DIGEST_A,
            "bounds_digest": DIGEST_B, "package_digest": DIGEST_A,
            "policy_digest": DIGEST_B, "agent_digest": DIGEST_C,
            "skill_digests": [DIGEST_A], "prompt_digest": DIGEST_B,
            "client_digest": content_digest(self.clients["codex-host-v1"]),
        }
        return self.ledger.open_host_run(
            self.project_id,
            assignment=assigned,
            task={
                "id": "task-1", "objective": "Read one fixture", "family": "research",
                "complexity": "medium", "risk": "low", "meaningful": True,
                **routing_fields(),
                "deterministic_validation": True, "independent_parts": 1,
                "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
            },
            route={
                "task_id": "task-1", "role": assigned["role"],
                "model": assigned["model"], "effort": assigned["effort"],
                "topology": "single", "max_parallel": 1,
                "verification_required": True, "provider": provider,
                "execution_configuration": configuration,
            },
            execution_configuration=configuration,
            client_version="codex-host-v1",
            orchestration_handle=orchestration["orchestration_handle"],
            now_ms=stamp,
        )

    def _launch_rows(self) -> dict[str, list[tuple]]:
        return {name: [tuple(row) for row in self.ledger._connection.execute(f"SELECT * FROM {name} ORDER BY rowid")]
                for name in ("host_run_bindings", "sessions", "run_provenance", "records", "record_links")}

    def _capture_open_arguments(self) -> tuple[dict, dict]:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        with mock.patch.object(self.ledger, "open_host_run", wraps=self.ledger.open_host_run) as opened:
            run = self.open_run()
        clock = mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 2)
        clock.start()
        self.addCleanup(clock.stop)
        return run, opened.call_args.kwargs

    def _reopen_launch_fixture(self) -> None:
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.ledger._clock_ms = lambda: self.clock + 2

    def _prepared_fixture_attestations(self):
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        return self.ledger._prepare_attestations(self.project_id, "codex")

    def test_open_prepares_signatures_without_a_write_transaction_and_does_not_reverify_at_commit(self) -> None:
        self._prepared_fixture_attestations()
        verify = OwnerGrantAuthority.verify_owner_record
        observed = []

        def checked(authority, project_id, record, signature):
            observed.append(self.ledger._connection.in_transaction)
            return verify(authority, project_id, record, signature)

        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=checked), \
                mock.patch.object(self.ledger, "_attestation_signed_by_owner_locked", side_effect=AssertionError("locked crypto")):
            self.assertEqual("admitted", self.open_run()["admission"])
        self.assertEqual([False], observed)

    def test_b_can_commit_registration_while_a_signature_verifier_is_blocked(self) -> None:
        self._prepared_fixture_attestations()
        entered, release, b_done = threading.Event(), threading.Event(), threading.Event()
        errors, proofs, registrations = [], [], []
        verify = OwnerGrantAuthority.verify_owner_record
        other = Path(self.temporary.name) / "project-during-signature"
        other.mkdir()

        def blocked(authority, project_id, record, signature):
            self.assertFalse(self.ledger._connection.in_transaction)
            entered.set()
            if not release.wait(5):
                raise AssertionError("fixture verifier was not released")
            return verify(authority, project_id, record, signature)

        def prepare():
            try:
                proofs.append(self.ledger._prepare_attestations(self.project_id, "codex"))
            except BaseException as exc:
                errors.append(exc)

        def register():
            try:
                registrations.append(self.ledger.register_project(other, now_ms=NOW))
            except BaseException as exc:
                errors.append(exc)
            finally:
                b_done.set()

        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=blocked):
            a = threading.Thread(target=prepare, daemon=True)
            b = threading.Thread(target=register, daemon=True)
            a.start()
            try:
                self.assertTrue(entered.wait(2))
                b.start()
                self.assertTrue(b_done.wait(2), "B waited on A's signature verification")
            finally:
                release.set()
                a.join(5)
                if b.ident is not None:
                    b.join(5)
        self.assertFalse(a.is_alive())
        self.assertFalse(b.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(proofs[0].verified)
        self.assertEqual(registrations[0], self.ledger.project_for_root(other))

    def test_prepared_signature_inputs_cannot_change_before_admission(self) -> None:
        prepared = self._prepared_fixture_attestations()
        identifier, sequence, content, digest, source, signature = prepared.rows[0]
        changed_record = json.loads(content)
        changed_record["observed_at_ms"] += 1
        changed_content = canonical_json(changed_record)
        variants = (
            ("signature", signature + b"fixture-change"),
            ("source", "provider"),
            ("content", changed_content),
            ("signer", b""),
            ("sequence", sequence + 100_000),
            ("digest", "0" * 64),
        )
        for field, changed in variants:
            with self.subTest(field=field):
                before = self._launch_rows()
                with self.ledger._account_transaction():
                    if field == "signature":
                        self.ledger._connection.execute("UPDATE owner_attestations SET signature=? WHERE project_id=? AND subject_record_id=?",
                                                        (changed, self.project_id, identifier))
                    elif field == "source":
                        self.ledger._connection.execute("UPDATE records SET source=? WHERE project_id=? AND record_id=?", (changed, self.project_id, identifier))
                    elif field == "content":
                        self.ledger._connection.execute("UPDATE records SET content=?,content_digest=? WHERE project_id=? AND record_id=?",
                                                        (changed, hashlib.sha256(changed).hexdigest(), self.project_id, identifier))
                    elif field == "signer":
                        self.state.allowed_signers_path.write_bytes(changed)
                    elif field == "sequence":
                        self.ledger._connection.execute("UPDATE records SET source_sequence=? WHERE project_id=? AND record_id=?", (changed, self.project_id, identifier))
                    else:
                        self.ledger._connection.execute("UPDATE records SET content_digest=? WHERE project_id=? AND record_id=?", (changed, self.project_id, identifier))
                try:
                    with self.assertRaisesRegex(LedgerError, "preparation is stale|attestation is incompatible"):
                        self.ledger.evaluate_admission(self.project_id, provider="codex", environ=CLEAN,
                                                       now_ms=self.clock + 1, _prepared_attestations=prepared)
                finally:
                    self.state.allowed_signers_path.write_bytes(prepared.signer)
                    with self.ledger._account_transaction():
                        self.ledger._connection.execute("UPDATE records SET content=?,content_digest=?,source=?,source_sequence=? WHERE project_id=? AND record_id=?",
                                                        (content, digest, source, sequence, self.project_id, identifier))
                        self.ledger._connection.execute("UPDATE owner_attestations SET signature=? WHERE project_id=? AND subject_record_id=?",
                                                        (signature, self.project_id, identifier))
                self.assertEqual(before, self._launch_rows())

    def test_preparation_is_bound_to_owner_history_sequence_even_if_signer_bytes_repeat(self) -> None:
        prepared = self._prepared_fixture_attestations()
        self.assertTrue(prepared.verified)
        with self.ledger._account_transaction():
            # Fixture-only generation step, not a supported key-return operation.
            self.ledger._connection.execute("INSERT INTO owner_key_history(signer_line,reason,recorded_at_ms) VALUES(?,'replaced',?)",
                                            (prepared.owner[1], NOW))
        with self.assertRaisesRegex(LedgerError, "preparation is stale"):
            self.ledger.evaluate_admission(self.project_id, provider="codex", environ=CLEAN,
                                           now_ms=self.clock + 1, _prepared_attestations=prepared)
        self.assertEqual([], self._launch_rows()["host_run_bindings"])

    def test_preparation_is_rejected_after_owner_reopen_and_json_is_not_a_preparation(self) -> None:
        prepared = self._prepared_fixture_attestations()
        other = Path(self.temporary.name) / "wrong-preparation-project"
        other.mkdir()
        other_id = self.ledger.register_project(other, now_ms=NOW)
        for project, provider in ((other_id, "codex"), (self.project_id, "claude")):
            with self.assertRaisesRegex(LedgerError, "preparation is stale"):
                self.ledger.evaluate_admission(project, provider=provider, environ=CLEAN,
                                               now_ms=self.clock + 1, _prepared_attestations=prepared)
        self._reopen_launch_fixture()
        for value in (prepared, {"verified": True, "project_id": self.project_id}):
            with self.assertRaisesRegex(LedgerError, "preparation is stale"):
                self.ledger.evaluate_admission(self.project_id, provider="codex", environ=CLEAN,
                                               now_ms=self.clock + 1, _prepared_attestations=value)

    def test_preparing_a_signature_does_not_cache_the_shared_account_run_cap(self) -> None:
        self.limit("codex", max_admissions_per_window=1)
        prepared = self._prepared_fixture_attestations()
        other = Path(self.temporary.name) / "cap-project"
        other.mkdir()
        first = self.project_id
        self.project_id = self.ledger.register_project(other, now_ms=NOW)
        try:
            identity = self.observe()[0]
            self.attest("codex", identity["account_fingerprint"])
            self.open_run()
        finally:
            self.project_id = first
        with self.ledger._account_transaction():
            decision = self.ledger._evaluate_admission_locked(self.project_id, provider="codex", environ=CLEAN,
                                                             timestamp=self.clock + 1, _prepared_attestations=prepared)
        self.assertEqual(("deny", "window_run_cap_reached"), (decision["decision"], decision["reason_code"]))

    def test_preparing_a_signature_does_not_cache_restrictive_account_evidence(self) -> None:
        prepared = self._prepared_fixture_attestations()
        other = Path(self.temporary.name) / "restriction-project"
        other.mkdir()
        first = self.project_id
        self.project_id = self.ledger.register_project(other, now_ms=NOW)
        try:
            self.observe(**{"rateLimits.spendControlReached": True})
        finally:
            self.project_id = first
        with self.assertRaises(AdmissionDenied) as caught:
            self.ledger.evaluate_admission(self.project_id, provider="codex", environ=CLEAN,
                                           now_ms=self.clock + 1, _prepared_attestations=prepared)
        self.assertEqual("codex_spend_control_reached", caught.exception.reason_code)

    def test_database_mode_check_fault_rolls_back_open_intent_and_result_before_commit(self) -> None:
        run, arguments = self._capture_open_arguments()
        harden = self.state.harden_database_files

        def refuse_writer():
            if self.ledger._connection.in_transaction:
                raise StateError("fixture database mode refusal")
            harden()

        before = self._launch_rows()
        with mock.patch.object(self.state, "harden_database_files", side_effect=refuse_writer), \
                self.assertRaisesRegex(StateError, "fixture database mode"):
            self.ledger.open_host_run(self.project_id, **arguments)
        self.assertFalse(self.ledger._connection.in_transaction)
        self._reopen_launch_fixture()
        self.assertEqual(before, self._launch_rows())
        intent_arguments = {key: arguments[key] for key in ("assignment", "task", "route")}
        intent_arguments.update(run_handle=run["run_handle"], deadline_seconds=600)
        with mock.patch.object(self.state, "harden_database_files", side_effect=refuse_writer), \
                self.assertRaisesRegex(StateError, "fixture database mode"):
            self.ledger.record_dispatch_intent(self.project_id, **intent_arguments)
        self.assertFalse(self.ledger._connection.in_transaction)
        self._reopen_launch_fixture()
        self.assertEqual(before, self._launch_rows())
        intent = self.ledger.record_dispatch_intent(self.project_id, **intent_arguments)
        before = self._launch_rows()
        result_arguments = dict(run_handle=run["run_handle"], bridge_run_id=intent["bridge_run_id"],
                                result={"fixture": "fenced"}, fenced=True, reviewed=False, terminal_state="failed",
                                result_validation_reason_code="invalid_shape")
        with mock.patch.object(self.state, "harden_database_files", side_effect=refuse_writer), \
                self.assertRaisesRegex(StateError, "fixture database mode"):
            self.ledger.record_dispatch_result(self.project_id, **result_arguments)
        self.assertFalse(self.ledger._connection.in_transaction)
        self._reopen_launch_fixture()
        self.assertEqual(before, self._launch_rows())
        self.ledger.record_dispatch_result(self.project_id, **result_arguments)

    def test_account_unit_rolls_back_base_exception_and_actual_commit_failure(self) -> None:
        class FixtureStop(BaseException):
            pass

        before = self._launch_rows()
        with self.assertRaises(FixtureStop), self.ledger._account_transaction():
            self.ledger._connection.execute("UPDATE projects SET created_at_ms=2 WHERE project_id=?", (self.project_id,))
            raise FixtureStop()
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual(1, self.ledger._connection.execute("SELECT created_at_ms FROM projects WHERE project_id=?", (self.project_id,)).fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError), self.ledger._account_transaction():
            self.ledger._connection.execute("PRAGMA defer_foreign_keys=ON")
            self.ledger._connection.execute("UPDATE projects SET created_at_ms=2 WHERE project_id=?", (self.project_id,))
            self.ledger._connection.execute("INSERT INTO record_links VALUES(?,?,?,'derived_from')", (self.project_id, "missing-source", "missing-target"))
        self.assertFalse(self.ledger._connection.in_transaction)
        self._reopen_launch_fixture()
        self.assertEqual(before, self._launch_rows())
        self.assertEqual(1, self.ledger._connection.execute("SELECT created_at_ms FROM projects WHERE project_id=?", (self.project_id,)).fetchone()[0])

    def test_mode_fault_rolls_back_initial_and_locked_admission_denials(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        other = Path(self.temporary.name) / "denial-mode-project"
        other.mkdir()
        other_id = self.ledger.register_project(other, now_ms=NOW)
        harden = self.state.harden_database_files

        def refuse_writer():
            if self.ledger._connection.in_transaction:
                raise StateError("fixture denial mode refusal")
            harden()

        before = self._launch_rows()
        with mock.patch.object(self.state, "harden_database_files", side_effect=refuse_writer), \
                self.assertRaisesRegex(StateError, "fixture denial mode"):
            self.ledger.evaluate_admission(other_id, provider="codex", environ=CLEAN, now_ms=self.clock + 1)
        self.assertFalse(self.ledger._connection.in_transaction)
        self._reopen_launch_fixture()
        self.assertEqual(before, self._launch_rows())
        orchestration = self.ledger.open_orchestration(self.project_id, client_version="codex-host-v1", now_ms=NOW)
        fresh = self.clock + 1
        stale = identity["observed_at_ms"] + IDENTITY_MAX_AGE_MS + 1
        times = iter((fresh, stale))
        with mock.patch.object(self.ledger, "_clock_ms", side_effect=lambda: next(times)), \
                mock.patch.object(self.state, "harden_database_files", side_effect=refuse_writer), \
                self.assertRaisesRegex(StateError, "fixture denial mode"):
            self._open_run("codex", orchestration, fresh)
        self.assertFalse(self.ledger._connection.in_transaction)
        self._reopen_launch_fixture()
        self.assertEqual(before, self._launch_rows())

    def test_late_open_failure_rolls_back_binding_provenance_events_and_admission(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        orchestration = self.ledger.open_orchestration(self.project_id, client_version="codex-host-v1", now_ms=NOW)
        before = self._launch_rows()
        append = self.ledger._store_admission_locked

        def fail_after_admission(*args, **kwargs):
            append(*args, **kwargs)
            raise sqlite3.OperationalError("fixture late admission fault")

        stamp = self.clock + 1
        with mock.patch.object(self.ledger, "_clock_ms", return_value=stamp), \
                mock.patch.object(self.ledger, "_store_admission_locked", side_effect=fail_after_admission), \
                self.assertRaisesRegex(sqlite3.OperationalError, "fixture late"):
            self._open_run("codex", orchestration, stamp)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual(before, self._launch_rows())
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.assertEqual(before, self._launch_rows())
        with mock.patch.object(self.ledger, "_clock_ms", return_value=stamp):
            run = self._open_run("codex", orchestration, stamp)
        self.assertEqual("admitted", run["admission"])
        self.assertEqual(1, len(self.ledger._prior_run_admissions_locked("codex", stamp)))

    def test_initial_denial_append_fault_rolls_back_and_retry_commits_only_the_denial(self) -> None:
        orchestration = self.ledger.open_orchestration(self.project_id, client_version="codex-host-v1", now_ms=NOW)
        before = self._launch_rows()
        append = self.ledger._append_checked_locked

        def fail_after_append(*args, **kwargs):
            append(*args, **kwargs)
            raise sqlite3.OperationalError("fixture initial denial fault")

        stamp = self.clock + 1
        with mock.patch.object(self.ledger, "_clock_ms", return_value=stamp), \
                mock.patch.object(self.ledger, "_append_checked_locked", side_effect=fail_after_append), \
                self.assertRaisesRegex(sqlite3.OperationalError, "fixture initial denial"):
            self._open_run("codex", orchestration, stamp)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual(before, self._launch_rows())
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.assertEqual(before, self._launch_rows())
        with mock.patch.object(self.ledger, "_clock_ms", return_value=stamp), self.assertRaises(AdmissionDenied) as caught:
            self._open_run("codex", orchestration, stamp)
        self.assertEqual("provider_identity_missing", caught.exception.reason_code)
        after = self._launch_rows()
        self.assertEqual(before["host_run_bindings"], after["host_run_bindings"])
        self.assertEqual(before["sessions"], after["sessions"])
        self.assertEqual(before["run_provenance"], after["run_provenance"])
        self.assertEqual(1, len(after["records"]) - len(before["records"]))
        denial = json.loads(after["records"][-1][8])
        self.assertEqual(("admission_decision", "deny", None), (denial["record_type"], denial["decision"], denial["run_id"]))
        self.assertFalse(self.ledger._connection.in_transaction)

    def test_account_unit_rolls_back_a_real_outcome_lineage_insert(self) -> None:
        outcome = verification_outcome()
        event = event_record("evt-account-lineage")
        event.update(run_id=outcome["run_id"], assignment_id=outcome["assignment_id"],
                     item_digest=outcome["evidence_digests"][0])
        with self.ledger._account_transaction():
            self.ledger._append_checked_locked(self.project_id, validate_learning_record(event), producer="host", timestamp=1000)
        before = self._launch_rows()
        with self.assertRaisesRegex(RuntimeError, "fixture lineage fault"), self.ledger._account_transaction():
            self.ledger._append_checked_locked(self.project_id, validate_learning_record(outcome), producer="verifier", timestamp=1001)
            links = self._launch_rows()["record_links"]
            self.assertEqual(1, len(links) - len(before["record_links"]))
            self.assertEqual((self.project_id, event["record_id"], outcome["record_id"], "derived_from"), links[-1])
            raise RuntimeError("fixture lineage fault")
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual(before, self._launch_rows())
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
        self.assertEqual(before, self._launch_rows())

    def test_intent_and_result_append_faults_roll_back_before_retry(self) -> None:
        run, arguments = self._capture_open_arguments()
        intent_arguments = {key: arguments[key] for key in ("assignment", "task", "route")}
        intent_arguments.update(run_handle=run["run_handle"], deadline_seconds=600)
        append = self.ledger._append_checked_locked

        def fail_after_append(*args, **kwargs):
            append(*args, **kwargs)
            raise sqlite3.OperationalError("fixture append fault")

        before = self._launch_rows()
        with mock.patch.object(self.ledger, "_append_checked_locked", side_effect=fail_after_append), \
                self.assertRaisesRegex(sqlite3.OperationalError, "fixture append"):
            self.ledger.record_dispatch_intent(self.project_id, **intent_arguments)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual(before, self._launch_rows())
        self._reopen_launch_fixture()
        self.assertEqual(before, self._launch_rows())
        append = self.ledger._append_checked_locked
        intent = self.ledger.record_dispatch_intent(self.project_id, **intent_arguments)
        with self.assertRaisesRegex(LedgerError, "already dispatched"):
            self.ledger.record_dispatch_intent(self.project_id, **intent_arguments)
        result_arguments = dict(run_handle=run["run_handle"], bridge_run_id=intent["bridge_run_id"],
                                result={"fixture": "validated elsewhere"}, fenced=True, reviewed=False, terminal_state="failed",
                                result_validation_reason_code="invalid_shape")
        before = self._launch_rows()
        with mock.patch.object(self.ledger, "_append_checked_locked", side_effect=fail_after_append), \
                self.assertRaisesRegex(sqlite3.OperationalError, "fixture append"):
            self.ledger.record_dispatch_result(self.project_id, **result_arguments)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual(before, self._launch_rows())
        self._reopen_launch_fixture()
        self.assertEqual(before, self._launch_rows())
        self.ledger.record_dispatch_result(self.project_id, **result_arguments)
        with self.assertRaisesRegex(LedgerError, "already recorded"):
            self.ledger.record_dispatch_result(self.project_id, **result_arguments)

    def test_account_transaction_refuses_nesting_without_committing_or_rolling_back_outer_work(self) -> None:
        connection = self.ledger._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("UPDATE projects SET created_at_ms=2 WHERE project_id=?", (self.project_id,))
            with self.assertRaisesRegex(LedgerError, "already active"), self.ledger._account_transaction():
                self.fail("nested account write entered")
            operations = (
                lambda: self._open_run("codex", {"orchestration_handle": "x"}, self.clock + 1),
                lambda: self.ledger.evaluate_admission(self.project_id, provider="codex", environ=CLEAN, now_ms=self.clock + 1),
                lambda: self.ledger.record_dispatch_intent(self.project_id, run_handle="x", assignment={}, task={}, route={}, deadline_seconds=600),
                lambda: self.ledger.record_dispatch_result(self.project_id, run_handle="x", bridge_run_id="x", result={}, fenced=True,
                                                          reviewed=False, terminal_state="failed"),
            )
            for operation in operations:
                with self.assertRaisesRegex(LedgerError, "already active"):
                    operation()
                self.assertTrue(connection.in_transaction)
            self.assertTrue(connection.in_transaction)
            self.assertEqual(2, connection.execute("SELECT created_at_ms FROM projects WHERE project_id=?", (self.project_id,)).fetchone()[0])
        finally:
            connection.rollback()
        self.assertEqual(1, connection.execute("SELECT created_at_ms FROM projects WHERE project_id=?", (self.project_id,)).fetchone()[0])

    def test_sigkill_before_and_after_open_commit_has_one_durable_boundary(self) -> None:
        _, arguments = self._capture_open_arguments()
        script = r'''
import json, os, signal, sys
from pathlib import Path
import tests.support
from codex_agents.learning_store import LearningLedger, LearningState
arguments = json.loads(sys.stdin.buffer.read())
ledger = LearningLedger(LearningState(Path(sys.argv[1])))
ledger._clock_ms = lambda: arguments["now_ms"]
append = ledger._store_admission_locked
def cut(*args, **kwargs):
    append(*args, **kwargs)
    print("before-commit", flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
if sys.argv[3] == "before-commit":
    ledger._store_admission_locked = cut
ledger.open_host_run(sys.argv[2], **arguments)
print("after-commit", flush=True)
os.kill(os.getpid(), signal.SIGKILL)
'''
        for phase in ("before-commit", "after-commit"):
            with self.subTest(phase=phase):
                before = self._launch_rows()
                self.ledger.close()
                try:
                    child = subprocess.run([sys.executable, "-B", "-c", script, str(self.state.root), self.project_id, phase],
                                           input=canonical_json(arguments), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           cwd=Path(__file__).resolve().parent.parent, timeout=15, check=False)
                finally:
                    self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
                self.assertEqual(-signal.SIGKILL, child.returncode, child.stderr.decode(errors="replace"))
                self.assertEqual(phase, child.stdout.decode().strip())
                after = self._launch_rows()
                if phase == "before-commit":
                    self.assertEqual(before, after)
                else:
                    self.assertEqual(1, len(after["host_run_bindings"]) - len(before["host_run_bindings"]))
                    self.assertEqual(1, len(after["sessions"]) - len(before["sessions"]))
                    self.assertEqual(1, len(after["run_provenance"]) - len(before["run_provenance"]))
                    new_records = [json.loads(row[8]) for row in after["records"][len(before["records"]):]]
                    self.assertEqual(["event", "admission_decision"], [record["record_type"] for record in new_records])
                    self.assertEqual(new_records[0]["run_id"], new_records[1]["run_id"])
                self.assertFalse(self.ledger._connection.in_transaction)

    def test_dispatch_preflight_never_takes_a_sqlite_write_reservation(self) -> None:
        run, arguments = self._capture_open_arguments()
        intent_arguments = {key: arguments[key] for key in ("assignment", "task", "route")}
        statements = []
        self.ledger._connection.set_trace_callback(statements.append)
        before = self._launch_rows()
        try:
            self.ledger.record_dispatch_intent(self.project_id, run_handle=run["run_handle"],
                                             deadline_seconds=600, claim=False, **intent_arguments)
            decision = self.ledger.evaluate_admission(self.project_id, provider="codex", environ=CLEAN, now_ms=self.clock + 2)
            self.assertEqual("pass", decision["decision"])
        finally:
            self.ledger._connection.set_trace_callback(None)
        self.assertEqual(before, self._launch_rows())
        self.assertFalse(any(statement.startswith("BEGIN") for statement in statements))
        with self.assertRaisesRegex(LedgerError, "claim flag"):
            self.ledger.record_dispatch_intent(self.project_id, run_handle=run["run_handle"],
                                             deadline_seconds=600, claim=1, **intent_arguments)

    def test_codex_denials_follow_the_fixed_order(self) -> None:
        self.assert_denied("provider_api_key_present", environ={"OPENAI_API_KEY": "x"})
        self.assert_denied(
            "provider_api_key_present", environ={"ANTHROPIC_API_KEY": "x"}
        )
        self.assert_denied("provider_identity_missing")
        self.observe(now_ms=NOW - IDENTITY_MAX_AGE_MS - 1)
        self.assert_denied("provider_identity_missing")
        identity = self.observe()[0]
        # The intended compatibility break: Codex-only projects need an attestation.
        self.assert_denied("billing_attestation_missing")
        self.attest("codex", identity["account_fingerprint"])
        self.observe(**{"rateLimits.credits": None})
        self.assert_denied("billing_unverified")
        soon = (self.clock + 5_000) // 1000
        self.observe(**{
            "rateLimits.secondary.usedPercent": 90, "rateLimits.secondary.resetsAt": soon,
        })
        self.assert_denied("provider_exhausted")
        # A later, favourable read does not clear a window that has not reset.
        self.observe()
        self.assert_denied("provider_exhausted")
        self.clock += 10_000
        self.observe(ordinaryUsageAllowed=False)
        self.assert_denied("provider_exhausted")
        latest = self.observe()
        decision = self.admit()
        self.assertEqual(
            ("pass", "admitted", "evidence"),
            (decision["decision"], decision["reason_code"], decision["admission_basis"]),
        )
        for record in latest:
            self.assertIn(content_digest(record), decision["evidence_digests"])
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("paid_usage_usable")
        # Paid usage seen within a day keeps denying, whatever comes after it.
        self.observe()
        self.assert_denied("paid_usage_usable")
        self.clock += 24 * 60 * 60 * 1000 + 1
        self.observe()
        self.assertEqual("pass", self.admit()["decision"])

    def credits_attest(self, fingerprint: str, issued_at: int = 1) -> None:
        attest_provider(
            self.ledger, self.project_id, self.owner_private_key, "codex", fingerprint,
            issued_at=issued_at, credits_spending_disabled=True,
        )

    def test_attested_disabled_credits_are_admitted_and_rechecked(self) -> None:
        identity = self.observe()[0]
        fingerprint = identity["account_fingerprint"]
        self.credits_attest(fingerprint)
        self.observe(**{"rateLimits.credits.hasCredits": True})
        decision = self.admit()
        self.assertEqual(("pass", "admitted"), (decision["decision"], decision["reason_code"]))
        # The observation itself still records credits as usable.
        with self.ledger.repository(self.project_id) as repository:
            stored = repository.records("provider_billing")[-1]
        self.assertEqual(
            (True, True, False, None),
            (stored["has_credits"], stored["paid_usage_usable"], stored["unlimited"],
             stored["spend_control_reached"]),
        )
        run = self.open_run()
        after = self.observe(**{"rateLimits.credits.hasCredits": True})[0]
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            self.assertIsNone(self.ledger.launch_recheck(
                self.project_id, run_handle=run["run_handle"], identity=after
            ))
        self.observe(**{"rateLimits.credits.unlimited": True})
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            self.assertEqual("paid_usage_usable", self.ledger.launch_recheck(
                self.project_id, run_handle=run["run_handle"], identity=after
            ))

    def test_credits_without_the_attested_field_are_denied(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("paid_usage_usable")

    def test_unlimited_credits_are_denied_even_with_the_field(self) -> None:
        identity = self.observe()[0]
        self.credits_attest(identity["account_fingerprint"])
        self.observe(**{
            "rateLimits.credits.hasCredits": True, "rateLimits.credits.unlimited": True,
        })
        self.assert_denied("paid_usage_usable")

    def test_spend_control_reached_is_denied_with_its_own_reason(self) -> None:
        identity = self.observe()[0]
        self.credits_attest(identity["account_fingerprint"])
        self.observe(**{
            "rateLimits.credits.hasCredits": True, "rateLimits.spendControlReached": True,
        })
        self.assert_denied("codex_spend_control_reached")
        # Without credits, and without the field, it denies the same way.
        self.observe(**{"rateLimits.spendControlReached": True})
        self.assert_denied("codex_spend_control_reached")
        with self.ledger.repository(self.project_id) as repository:
            self.assertIs(True, repository.records("provider_billing")[-1]["spend_control_reached"])

    def test_credits_attestation_for_another_account_does_not_apply(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.credits_attest("0" * 64, issued_at=2)
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("paid_usage_usable")

    def test_the_credits_field_is_for_a_codex_grant_and_must_be_true(self) -> None:
        grant = {
            "version": 1, "provider": "claude", "account_fingerprint": "0" * 64,
            "no_paid_overage": True, "no_usage_credits": True, "issued_at": 5,
            "codex_credits_spending_disabled": True,
        }
        with self.assertRaisesRegex(ContractError, "only for a Codex grant"):
            billing_attestation_record(grant)
        with self.assertRaisesRegex(ContractError, "must be true"):
            billing_attestation_record(
                {**grant, "provider": "codex", "codex_credits_spending_disabled": False}
            )
        record = billing_attestation_record({**grant, "provider": "codex"})
        self.assertIs(True, record["codex_credits_spending_disabled"])
        older = {key: value for key, value in grant.items()
                 if key != "codex_credits_spending_disabled"}
        self.assertNotIn(
            "codex_credits_spending_disabled", billing_attestation_record(older)
        )

    def test_launch_recheck_function_honours_the_field(self) -> None:
        fingerprint = "a" * 64
        identity = {"account_fingerprint": fingerprint, "plan_type": "plus"}

        def billing(**changes) -> list:
            record = {
                "provider": "codex", "account_fingerprint": fingerprint,
                "paid_usage_usable": True, "has_credits": True, "unlimited": False,
                "spend_control_reached": False, "is_using_overage": None,
                "ordinary_usage_allowed": True, "observed_at_ms": NOW, **changes,
            }
            return [(record, DIGEST_C)]

        def attestation(fingerprint: str = fingerprint, **changes) -> list:
            record = {"provider": "codex", "account_fingerprint": fingerprint,
                      "observed_at_ms": 1, "codex_credits_spending_disabled": True, **changes}
            return [(record, DIGEST_B)]

        def launch(records, attestations):
            return evaluate_launch(
                provider="codex", identity=identity, billing=records, headroom=[],
                now_ms=NOW + 1, attestations=attestations,
            )

        self.assertIsNone(launch(billing(), attestation()))
        self.assertEqual("paid_usage_usable", launch(billing(), attestation(**{
            "codex_credits_spending_disabled": None})))
        self.assertEqual("paid_usage_usable", launch(billing(), None))
        self.assertEqual("paid_usage_usable", launch(billing(), attestation("b" * 64)))
        self.assertEqual("paid_usage_usable", launch(billing(unlimited=True), attestation()))
        self.assertEqual(
            "codex_spend_control_reached",
            launch(billing(spend_control_reached=True), attestation()),
        )

    def test_codex_without_recent_billing_is_denied(self) -> None:
        identity = parse_claude(
            provider_fixture("claude-auth-status.json"), digest_key=KEY, observed_at_ms=NOW
        )
        codex_identity = {**identity, "provider": "codex", "auth_method": None,
                          "subscription_type": None, "plan_type": "plus",
                          "client_version": "0.164.0"}
        attestation = {"provider": "codex",
                       "account_fingerprint": identity["account_fingerprint"],
                       "observed_at_ms": 1}
        decision = evaluate_admission(
            provider="codex",
            identities=[(codex_identity, DIGEST_A)],
            attestations=[(attestation, DIGEST_B)],
            billing=[], headroom=[], prior_claude_admissions=set(),
            environ={}, now_ms=NOW + 1, **CODEX_LIMITS,
        )
        self.assertEqual("billing_unverified", decision["reason_code"])
        stale_billing = {"provider": "codex",
                         "account_fingerprint": identity["account_fingerprint"],
                         "paid_usage_usable": False, "is_using_overage": None,
                         "ordinary_usage_allowed": True,
                         "observed_at_ms": NOW - IDENTITY_MAX_AGE_MS}
        decision = evaluate_admission(
            provider="codex",
            identities=[(codex_identity, DIGEST_A)],
            attestations=[(attestation, DIGEST_B)],
            billing=[(stale_billing, DIGEST_C)], headroom=[],
            prior_claude_admissions=set(), environ={}, now_ms=NOW + 1, **CODEX_LIMITS,
        )
        self.assertEqual("billing_unverified", decision["reason_code"])

    def test_claude_requires_subscription_login(self) -> None:
        console = self.observe("claude", authMethod="console", subscriptionType=None)[0]
        self.attest("claude", console["account_fingerprint"])
        self.assert_denied("claude_subscription_missing", "claude")
        self.observe("claude", subscriptionType="fixture-org-7f3a91")
        self.assert_denied("claude_subscription_missing", "claude")
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.assertEqual(console["account_fingerprint"], fingerprint)
        self.assertEqual("pass", self.admit("claude")["decision"])

    def test_claude_bootstrap_admits_one_run_until_it_records_billing(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        first = self.open_run("claude")
        stored = self.ledger.host_run_admission(self.project_id, first["run_handle"])
        self.assertEqual("bootstrap", stored["admission_basis"])
        # A second concurrent bootstrap is denied until the first records billing.
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run("claude")
        self.assertEqual("billing_unverified", raised.exception.reason_code)
        self.event(first, status="allowed", rateLimitType="five_hour",
                   utilization=0.1, isUsingOverage=True)
        self.assert_denied("paid_usage_usable", "claude")

    def test_bootstrap_then_evidence_based_admission(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        first = self.open_run("claude")
        self.event(first, **json.loads(provider_fixture("claude-rate-limit-event.json")))
        second = self.open_run("claude")
        stored = self.ledger.host_run_admission(self.project_id, second["run_handle"])
        self.assertEqual("evidence", stored["admission_basis"])

    def test_fabricated_favourable_event_cannot_clear_a_restriction(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        bootstrap = self.open_run("claude")
        self.event(bootstrap, status="allowed", rateLimitType="five_hour",
                   utilization=0.2, isUsingOverage=False, overageStatus="rejected",
                   overageDisabledReason="org_level_disabled")
        real, forger = self.open_run("claude"), self.open_run("claude")
        future = (self.clock + 3_600_000) // 1000
        self.event(real, status="rejected", rateLimitType="seven_day",
                   utilization=1.0, resetsAt=future, isUsingOverage=False,
                   overageStatus="rejected", overageDisabledReason="org_level_disabled")
        self.assert_denied("provider_exhausted", "claude")
        self.event(forger, status="allowed", rateLimitType="seven_day",
                   utilization=0.0, resetsAt=future, isUsingOverage=False,
                   overageStatus="rejected", overageDisabledReason="org_level_disabled")
        self.assert_denied("provider_exhausted", "claude")
        # A run keeps only events more restrictive than its own earlier ones,
        # so a later favourable event is dropped rather than recorded.
        self.assertEqual([], self.event(
            real, status="allowed", rateLimitType="seven_day", utilization=0.0,
            resetsAt=future, isUsingOverage=False, overageStatus="rejected",
            overageDisabledReason="org_level_disabled",
        ))
        self.clock += 3_600_000
        self.clean_claude_billing(self.tick())
        self.assertEqual("pass", self.admit("claude")["decision"])

    def test_committed_or_closed_runs_cannot_record_events(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        # Committing a result consumes the binding; emulate that final state.
        with self.ledger._connection:
            self.ledger._connection.execute(
                "UPDATE host_run_bindings SET consumed_at_ms=?,binding_state='terminal' "
                "WHERE project_id=?",
                (self.clock, self.project_id),
            )
        with self.assertRaisesRegex(LedgerError, "no longer open"):
            self.event(run, status="allowed")
        with self.assertRaisesRegex(LedgerError, "unavailable"):
            self.event({"run_handle": "hrn_" + "0" * 48}, status="allowed")
        with self.assertRaises(TypeError):
            record_claude_rate_limit_event(
                self.ledger, self.project_id, run_handle=run["run_handle"],
                rate_limit_info={"status": "allowed"}, observed_at_ms=1,
            )

    def test_bootstrap_admits_on_identity_and_attestation_alone(self) -> None:
        identity = self.observe("claude")[0]
        self.attest("claude", identity["account_fingerprint"])
        decision = self.admit("claude")
        self.assertEqual(("pass", "bootstrap"), (decision["decision"], decision["admission_basis"]))
        self.assertEqual(2, len(decision["evidence_digests"]))

    def test_attestation_for_another_account_does_not_count(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", DIGEST_C)
        self.assert_denied("billing_attestation_missing")
        self.attest("claude", identity["account_fingerprint"])
        self.assert_denied("billing_attestation_missing")

    def test_open_host_run_always_admits_inside_the_ledger(self) -> None:
        # Blocker 1: no caller-supplied decision is accepted, and none is needed.
        with self.assertRaises(TypeError):
            self.ledger.open_host_run(
                self.project_id, assignment={}, task={}, route={},
                execution_configuration={}, client_version="codex-host-v1",
                orchestration_handle="x",
                admission={"decision": "pass", "provider": "codex"},
            )
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run()
        self.assertEqual("provider_identity_missing", raised.exception.reason_code)
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "x"}):
            with self.assertRaises(AdmissionDenied) as raised:
                self.open_run()
        self.assertEqual("provider_api_key_present", raised.exception.reason_code)
        opened = self.open_run()
        self.assertEqual("admitted", opened["admission"])
        stored = self.ledger.host_run_admission(self.project_id, opened["run_handle"])
        self.assertEqual(identity["account_fingerprint"], stored["account_fingerprint"])

    def test_forged_pass_without_evidence_is_rejected(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        opened = self.open_run()
        forged = {
            "schema_version": 1, "record_type": "admission_decision",
            "record_id": "admission_decision:forged", "run_id": "run:forged",
            "provider": "codex", "decision": "pass", "reason_code": "admitted",
            "admission_basis": "evidence",
            "account_fingerprint": None, "evidence_digests": [],
            "observed_at_ms": NOW,
        }
        with self.assertRaisesRegex(ContractError, "fingerprint, and evidence"):
            validate_learning_record(forged)
        with self.assertRaises(LedgerError):
            self.ledger.append(self.project_id, forged, producer="lifecycle")
        plausible = {**forged, "account_fingerprint": DIGEST_A, "evidence_digests": [DIGEST_B]}
        for producer in ("lifecycle", "host", "evaluator"):
            with self.subTest(producer=producer), self.assertRaises(LedgerError):
                self.ledger.append(self.project_id, plausible, producer=producer)
        # Even a row written straight into storage does not pass the replay check.
        with self.ledger._connection:
            self.ledger._connection.execute(
                "UPDATE records SET content=json_set(CAST(content AS TEXT),"
                "'$.account_fingerprint',NULL,'$.evidence_digests',json('[]')) "
                "WHERE project_id=? AND record_type='admission_decision'",
                (self.project_id,),
            )
        with self.assertRaisesRegex(LedgerError, "admission is invalid"):
            self.ledger.host_run_admission(self.project_id, opened["run_handle"])

    def test_forged_provider_observations_cannot_be_appended(self) -> None:
        # Blocker 3: a producer cannot copy a fingerprint into favourable records.
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("paid_usage_usable")
        favourable = parse_codex(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=self.state.digest_key(), observed_at_ms=self.tick(),
        )
        self.assertEqual(identity["account_fingerprint"], favourable[2]["account_fingerprint"])
        for producer in ("provider", "hook", "app_server", "host", "evaluator",
                         "verifier", "owner", "lifecycle"):
            for record in favourable:
                with self.subTest(producer=producer, record=record["record_type"]):
                    with self.assertRaises(LedgerError):
                        self.ledger.append(self.project_id, record, producer=producer)
        self.assertFalse(hasattr(self.ledger, "record_provider_observations"))
        self.assertNotIn("provider", PRODUCER_AUTHORITY)
        producers = json.loads(
            (ORCHESTRATION_ROOT / "config/learning-producers.json").read_text()
        )["producers"]
        self.assertFalse(
            any(set(types) & {"provider_identity", "provider_headroom", "provider_billing"}
                for types in producers.values())
        )
        self.assert_denied("paid_usage_usable")

    def test_collector_sessions_need_admission_to_commit(self) -> None:
        # Blocker 2: the legacy result path is tied to an admission taken at bind.
        clock = mock.patch.object(self.ledger, "_clock_ms", side_effect=lambda: self.clock + 1)
        clock.start()
        self.addCleanup(clock.stop)
        _, _, unadmitted = self.ledger.bind_session(
            self.project, "session-before-evidence", "app_server",
            "codex-cli-0.153.0", now_ms=self.clock + 1,
        )
        with self.ledger._lock, self.assertRaisesRegex(
            LedgerError, "no stored admission decision"
        ):
            self.ledger._run_admission_locked(self.project_id, unadmitted)
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        _, _, admitted = self.ledger.bind_session(
            self.project, "session-after-evidence", "app_server",
            "codex-cli-0.153.0", now_ms=self.clock + 1,
        )
        with self.ledger._lock:
            decision = self.ledger._run_admission_locked(self.project_id, admitted)
        self.assertEqual(identity["account_fingerprint"], decision["account_fingerprint"])
        # Rebinding an existing session never upgrades its admission.
        self.ledger.bind_session(
            self.project, "session-before-evidence", "app_server",
            "codex-cli-0.153.0", now_ms=self.clock + 2,
        )
        with self.ledger._lock, self.assertRaisesRegex(
            LedgerError, "no stored admission decision"
        ):
            self.ledger._run_admission_locked(self.project_id, unadmitted)

    def test_native_binding_rolls_back_provenance_and_pass_together_after_append_fault(self) -> None:
        self._prepared_fixture_attestations()
        before = self._launch_rows()
        append = self.ledger._store_admission_locked

        def fail_after_pass(*args, **kwargs):
            append(*args, **kwargs)
            raise sqlite3.OperationalError("fixture native admission fault")

        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1), \
                mock.patch.object(self.ledger, "_store_admission_locked", side_effect=fail_after_pass), \
                self.assertRaisesRegex(sqlite3.OperationalError, "fixture native"):
            self.ledger.bind_session(self.project, "atomic-native", "hook", "codex-native-fixture", now_ms=self.clock + 1)
        self.assertFalse(self.ledger._connection.in_transaction)
        self._reopen_launch_fixture()
        self.assertEqual(before, self._launch_rows())
        project, _, run = self.ledger.bind_session(self.project, "atomic-native", "hook", "codex-native-fixture", now_ms=self.clock + 2)
        with self.ledger._lock:
            self.assertEqual("pass", self.ledger._run_admission_locked(project, run)["decision"])

    def test_native_binding_known_and_new_producer_paths_never_reverify_or_charge_twice(self) -> None:
        self.limit("codex", max_admissions_per_window=1)
        self._prepared_fixture_attestations()
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            first = self.ledger.bind_session(self.project, "one-native-root", "hook", "codex-native-fixture")
        statements = []
        self.ledger._connection.set_trace_callback(statements.append)
        try:
            with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("repeated crypto")), \
                    mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("known binding did crypto")), \
                    mock.patch.object(self.ledger, "_account_transaction", side_effect=AssertionError("known binding took writer")):
                self.assertEqual(first, self.ledger.bind_session(self.project, "one-native-root", "hook", "codex-native-fixture"))
        finally:
            self.ledger._connection.set_trace_callback(None)
        self.assertFalse(any(sql.startswith(("BEGIN", "INSERT", "UPDATE", "DELETE")) for sql in statements))
        with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("producer rebinding did crypto")), \
                mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("producer rebinding verified crypto")), \
                mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 2):
            self.assertEqual(first, self.ledger.bind_session(self.project, "one-native-root", "app_server", "codex-native-fixture"))
        self.assertEqual(2, len(self._launch_rows()["run_provenance"]))
        with self.ledger._lock:
            self.assertEqual("pass", self.ledger._run_admission_locked(first[0], first[2])["decision"])
            self.assertEqual(1, len(self.ledger._prior_run_admissions_locked("codex", self.clock + 2)))

    def test_denied_native_root_is_not_upgraded_through_another_producer(self) -> None:
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            first = self.ledger.bind_session(self.project, "denied-native-root", "hook", "codex-native-fixture")
        self._prepared_fixture_attestations()
        with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("denied root re-admitted")), \
                mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 2):
            self.assertEqual(first, self.ledger.bind_session(self.project, "denied-native-root", "app_server", "codex-native-fixture"))
        with self.ledger._lock, self.assertRaisesRegex(LedgerError, "no stored admission decision"):
            self.ledger._run_admission_locked(first[0], first[2])
        self.assertEqual(2, len(self._launch_rows()["run_provenance"]))

    def test_native_binding_race_charges_one_root_once_across_producers(self) -> None:
        self.limit("codex", max_admissions_per_window=1)
        self._prepared_fixture_attestations()
        prepare = self.ledger._prepare_attestations
        barrier = threading.Barrier(2)
        bindings, errors = [], []

        def prepared_together(*args, **kwargs):
            value = prepare(*args, **kwargs)
            barrier.wait(timeout=5)
            return value

        def bind(producer):
            try:
                bindings.append(self.ledger.bind_session(self.project, "racing-native", producer, "codex-native-fixture"))
            except BaseException as exc:
                errors.append(exc)

        with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=prepared_together), \
                mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            workers = [threading.Thread(target=bind, args=(producer,), daemon=True) for producer in ("hook", "app_server")]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual([], errors)
        self.assertEqual(2, len(bindings))
        self.assertEqual(bindings[0], bindings[1])
        with self.ledger._lock:
            self.assertEqual("pass", self.ledger._run_admission_locked(bindings[0][0], bindings[0][2])["decision"])
            self.assertEqual(1, len(self.ledger._prior_run_admissions_locked("codex", self.clock + 2)))

    def test_native_binding_refuses_outer_work_before_preparation(self) -> None:
        connection = self.ledger._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("UPDATE projects SET created_at_ms=2 WHERE project_id=?", (self.project_id,))
            with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("crypto in outer writer")), \
                    self.assertRaisesRegex(LedgerError, "already active"):
                self.ledger.bind_session(self.project, "nested-native", "hook", "codex-native-fixture")
            self.assertTrue(connection.in_transaction)
            self.assertEqual(2, connection.execute("SELECT created_at_ms FROM projects WHERE project_id=?", (self.project_id,)).fetchone()[0])
        finally:
            connection.rollback()

    def test_native_binding_refuses_inconsistent_provenance_without_repair_or_admission(self) -> None:
        self._prepared_fixture_attestations()
        for field in ("client_version", "session_digest"):
            with self.subTest(field=field):
                identifier = "inconsistent-native-" + field
                with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
                    project, session, run = self.ledger.bind_session(self.project, identifier, "hook", "codex-native-fixture")
                with self.ledger._account_transaction():
                    if field == "client_version":
                        self.ledger._connection.execute("UPDATE run_provenance SET client_version='wrong-version' WHERE project_id=? AND run_id=?",
                                                        (project, run))
                    else:
                        self.ledger._connection.execute("UPDATE run_provenance SET session_digest='wrong-session' WHERE project_id=? AND run_id=?",
                                                        (project, run))
                before = self._launch_rows()
                try:
                    for producer in ("hook", "app_server"):
                        with self.subTest(producer=producer), self.assertRaisesRegex(LedgerError, "provenance conflicts"):
                            self.ledger.bind_session(self.project, identifier, producer, "codex-native-fixture")
                        self.assertEqual(before, self._launch_rows())
                finally:
                    with self.ledger._account_transaction():
                        self.ledger._connection.execute("UPDATE run_provenance SET client_version=?,session_digest=? WHERE project_id=? AND run_id=?",
                                                        ("codex-native-fixture", session, project, run))

    def test_native_binding_refuses_unknown_and_extra_root_producer_rows(self) -> None:
        self._prepared_fixture_attestations()
        for mode in ("unknown", "extra"):
            identifier = "unexpected-native-" + mode
            with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
                project, session, run = self.ledger.bind_session(self.project, identifier, "hook", "codex-native-fixture")
            if mode == "extra":
                self.ledger.bind_session(self.project, identifier, "app_server", "codex-native-fixture")
            with self.ledger._account_transaction():
                # A managed-host row is invalid in this native-root namespace.
                self.ledger._connection.execute("INSERT INTO run_provenance VALUES(?,?,?,?,?,?)",
                                                (project, run, "host", "codex-native-fixture", session, NOW))
            before = self._launch_rows()
            for producer in ("hook", "app_server"):
                with self.subTest(mode=mode, producer=producer), self.assertRaisesRegex(LedgerError, "provenance conflicts"):
                    self.ledger.bind_session(self.project, identifier, producer, "codex-native-fixture")
                self.assertEqual(before, self._launch_rows())

    def test_native_binding_sigkill_before_and_after_commit_has_one_durable_boundary(self) -> None:
        self._prepared_fixture_attestations()
        script = r'''
import os, signal, sys
from pathlib import Path
import tests.support
from codex_agents.learning_store import LearningLedger, LearningState
ledger = LearningLedger(LearningState(Path(sys.argv[1])))
stamp = int(sys.argv[4]); ledger._clock_ms = lambda: stamp
append = ledger._store_admission_locked
def cut(*args, **kwargs):
    append(*args, **kwargs)
    print("before-commit", flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
if sys.argv[3] == "before-commit":
    ledger._store_admission_locked = cut
ledger.bind_session(Path(sys.argv[2]), "kill-native", "hook", "codex-native-fixture", now_ms=stamp)
print("after-commit", flush=True)
os.kill(os.getpid(), signal.SIGKILL)
'''
        for phase in ("before-commit", "after-commit"):
            with self.subTest(phase=phase):
                before = self._launch_rows()
                self.ledger.close()
                try:
                    child = subprocess.run([sys.executable, "-B", "-c", script, str(self.state.root), str(self.project), phase, str(self.clock + 1)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           cwd=Path(__file__).resolve().parent.parent, timeout=15, check=False)
                finally:
                    self.ledger = LearningLedger(self.state, client_capabilities=self.clients)
                self.assertEqual(-signal.SIGKILL, child.returncode, child.stderr.decode(errors="replace"))
                self.assertEqual(phase, child.stdout.decode().strip())
                after = self._launch_rows()
                if phase == "before-commit":
                    self.assertEqual(before, after)
                else:
                    self.assertEqual(1, len(after["sessions"]) - len(before["sessions"]))
                    self.assertEqual(1, len(after["run_provenance"]) - len(before["run_provenance"]))
                    new_records = [json.loads(row[8]) for row in after["records"][len(before["records"]):]]
                    self.assertEqual(["admission_decision"], [record["record_type"] for record in new_records])
                    self.assertEqual(after["run_provenance"][-1][1], new_records[0]["run_id"])
                self.assertFalse(self.ledger._connection.in_transaction)

    def test_replay_reads_the_stored_decision_after_observations_go_stale(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        opened = self.open_run()
        decision = self.ledger.host_run_admission(self.project_id, opened["run_handle"])
        later = self.clock + IDENTITY_MAX_AGE_MS + 60_000
        self.assert_denied("provider_identity_missing", now_ms=later)
        self.assertEqual(
            decision, self.ledger.host_run_admission(self.project_id, opened["run_handle"])
        )
        self.assertEqual("pass", decision["decision"])

    def test_claude_rate_limit_events_use_the_runs_admitted_identity(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        opened = self.open_run("claude")
        records = self.event(
            opened, **json.loads(provider_fixture("claude-rate-limit-event.json"))
        )
        self.assertEqual(
            [fingerprint, fingerprint],
            [record["account_fingerprint"] for record in records],
        )
        self.assertEqual(self.clock, records[0]["observed_at_ms"])
        codex_identity = self.observe()[0]
        self.attest("codex", codex_identity["account_fingerprint"])
        codex_run = self.open_run()
        with self.assertRaisesRegex(ContractError, "not belong to a Claude run"):
            self.event(codex_run, status="allowed")

    def test_attestation_status_uses_current_owner_and_account_in_this_project(self) -> None:
        def report():
            return self.ledger.provider_attestation_status(self.project_id)
        self.assertEqual("identity_missing", report()["codex"]["state"])
        identity = self.observe()[0]
        self.assertEqual("missing", report()["codex"]["state"])
        self.attest("codex", "0" * 64)
        self.assertEqual("missing", report()["codex"]["state"])
        self.attest("codex", identity["account_fingerprint"])
        self.assertEqual("present", report()["codex"]["state"])
        other = Path(self.temporary.name) / "other-project"
        other.mkdir()
        other_id = self.ledger.register_project(other)
        self.assertEqual("identity_missing",
                         self.ledger.provider_attestation_status(other_id)["codex"]["state"])
        key, public = self.other_key()
        self.ledger.replace_owner_key(public)
        self.assertEqual("missing", report()["codex"]["state"])
        self.assertNotIn(identity["account_fingerprint"], json.dumps(report()))

    def test_attestations_require_an_owner_signature(self) -> None:
        identity = self.observe()[0]
        record = billing_attestation_record({
            "version": 1, "provider": "codex",
            "account_fingerprint": identity["account_fingerprint"],
            "no_paid_overage": True, "no_usage_credits": True, "issued_at": 5,
        })
        with self.assertRaisesRegex(LedgerError, "signature is invalid"):
            self.ledger.commit_owner_record(
                self.project_id, record=record, signature=b"not a signature"
            )
        with self.assertRaises(LedgerError):
            self.ledger.append(self.project_id, record, producer="owner")
        with self.assertRaisesRegex(ContractError, "no paid overage"):
            billing_attestation_record({
                "version": 1, "provider": "codex",
                "account_fingerprint": identity["account_fingerprint"],
                "no_paid_overage": False, "no_usage_credits": True, "issued_at": 5,
            })
        self.assert_denied("billing_attestation_missing")

    # Replacing a lost owner key.

    def other_key(self, name: str = "other") -> tuple[Path, str]:
        directory = Path(self.temporary.name) / name
        directory.mkdir()
        return generate_test_owner_key(directory)

    def denial(self) -> str:
        """The admission denial's reason, or "admitted"."""

        try:
            self.admit()
        except AdmissionDenied as denied:
            return denied.reason_code
        return "admitted"

    def test_an_emptied_signer_file_does_not_accept_another_key(self) -> None:
        identity = self.observe()[0]
        original = self.state.allowed_signers_path.read_bytes()
        self.state.allowed_signers_path.write_bytes(b"")
        key, public = self.other_key()
        with self.assertRaisesRegex(StateError, "Replace a lost owner key"):
            self.state.install_owner_public_key(public)
        with self.assertRaises((LedgerError, StateError)):
            attest_provider(
                self.ledger, self.project_id, key, "codex", identity["account_fingerprint"]
            )
        self.assert_denied("billing_attestation_missing")
        # The key on record may be put back.
        self.state.install_owner_public_key(original.decode("ascii").split(" ", 1)[1])
        self.assertEqual(self.state.allowed_signers_path.read_bytes(), original)
        self.assertEqual(self.state.owner_key_report()["state"], "ok")

    def test_a_swapped_signer_file_signs_nothing_and_stops_admission(self) -> None:
        identity = self.observe()[0]
        fingerprint = identity["account_fingerprint"]
        self.attest("codex", fingerprint)
        self.assertNotEqual(self.denial(), "billing_attestation_missing")
        original = self.state.allowed_signers_path.read_bytes()
        key, public = self.other_key()
        fields = public.split()
        self.state.allowed_signers_path.write_bytes(
            f"codex-agents-owner {fields[0]} {fields[1]}\n".encode("ascii")
        )
        with self.assertRaisesRegex(StateError, "not the owner key on record"):
            self.state.owner_allowed_signers()
        self.assertEqual(self.state.owner_key_report()["state"], "mismatch")
        self.assert_denied("billing_attestation_missing")
        with self.assertRaises((LedgerError, StateError)):
            attest_provider(self.ledger, self.project_id, key, "codex", fingerprint)
        # Putting the recorded key back restores what it signed.
        self.state.allowed_signers_path.write_bytes(original)
        self.assertNotEqual(self.denial(), "billing_attestation_missing")

    def test_replacing_the_key_cancels_what_the_old_key_signed(self) -> None:
        identity = self.observe()[0]
        fingerprint = identity["account_fingerprint"]
        self.attest("codex", fingerprint)
        self.assertNotEqual(self.denial(), "billing_attestation_missing")
        key, public = self.other_key()
        report = self.ledger.replace_owner_key(public, now_ms=NOW)
        self.assertEqual(report["state"], "ok")
        self.assertEqual(report["replacements"], 1)
        self.assertEqual(report["last_replaced_at_ms"], NOW)
        listed = subprocess.run(
            ["ssh-keygen", "-l", "-f", f"{key}.pub"], capture_output=True, text=True, check=True
        )
        self.assertEqual(report["fingerprint"], listed.stdout.split()[1])
        self.assert_denied("billing_attestation_missing")
        # The old key can no longer sign; the new one can.
        with self.assertRaises(LedgerError):
            attest_provider(
                self.ledger, self.project_id, self.owner_private_key, "codex", fingerprint,
                issued_at=2,
            )
        attest_provider(self.ledger, self.project_id, key, "codex", fingerprint, issued_at=3)
        self.assertNotEqual(self.denial(), "billing_attestation_missing")

    def test_an_interrupted_replacement_fails_closed_and_can_be_finished(self) -> None:
        self.observe()
        key, public = self.other_key()
        with mock.patch.object(
            self.state, "_write_owner_signers", side_effect=OSError("interrupted")
        ):
            with self.assertRaises(OSError):
                self.ledger.replace_owner_key(public, now_ms=NOW)
        self.assertEqual(self.state.owner_key_report()["state"], "mismatch")
        self.assert_denied("billing_attestation_missing")
        report = self.ledger.replace_owner_key(public, now_ms=NOW + 1)
        self.assertEqual(report["state"], "ok")
        self.assertEqual(report["replacements"], 1)
        self.assertEqual(report["last_replaced_at_ms"], NOW)

    def test_a_run_admitted_before_the_replacement_does_not_launch(self) -> None:
        identity = self.observe()[0]
        attestation = (
            {"provider": "codex", "account_fingerprint": identity["account_fingerprint"],
             "observed_at_ms": 1, "codex_credits_spending_disabled": True},
            DIGEST_B,
        )
        self.assertEqual(
            evaluate_launch(
                provider="codex", identity=identity, billing=[], headroom=[],
                now_ms=NOW + 1, attestations=[],
            ),
            "billing_attestation_missing",
        )
        self.assertNotEqual(
            evaluate_launch(
                provider="codex", identity=identity, billing=[], headroom=[],
                now_ms=NOW + 1, attestations=[attestation],
            ),
            "billing_attestation_missing",
        )

    def test_launch_recheck_refuses_after_the_key_is_replaced(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.observe()
        run = self.open_run()
        after = self.observe()[0]
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            self.assertIsNone(self.ledger.launch_recheck(
                self.project_id, run_handle=run["run_handle"], identity=after
            ))
        self.ledger.replace_owner_key(self.other_key()[1], now_ms=NOW)
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            self.assertEqual("billing_attestation_missing", self.ledger.launch_recheck(
                self.project_id, run_handle=run["run_handle"], identity=after
            ))

    def test_a_replaced_key_cannot_come_back(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        original = self.owner_private_key.with_suffix(".pub").read_text()
        _, public = self.other_key()
        self.ledger.replace_owner_key(public, now_ms=NOW)
        with self.assertRaisesRegex(LedgerError, "replaced earlier"):
            self.ledger.replace_owner_key(original, now_ms=NOW + 1)
        self.assert_denied("billing_attestation_missing")

    @staticmethod
    def key_blob(*parts: bytes) -> str:
        blob = b"".join(len(part).to_bytes(4, "big") + part for part in parts)
        return base64.b64encode(blob).decode("ascii")

    def test_a_public_key_must_parse_before_it_is_recorded(self) -> None:
        before = self.state.owner_key_report()
        good = self.owner_private_key.with_suffix(".pub").read_text().split()[1]
        raw = base64.b64decode(good)
        ed = b"ssh-ed25519"
        for bad in (
            "ssh-ed25519 AAAAA",
            "ssh-ed25519 QUJD",
            "ssh-rsa " + good,
            f"ssh-ed25519 {self.key_blob(ed, b'x' * 5)}",
            f"ssh-ed25519 {self.key_blob(ed)}",
            f"ssh-ed25519 {base64.b64encode(raw + b'tail').decode('ascii')}",
            f"ssh-ed25519 {self.key_blob(ed, b'x' * 32, b'extra')}",
            "ssh-rsa " + self.key_blob(b"ssh-rsa", b"\x01\x00\x01", b"\x7f" * 64),
            "ssh-rsa " + self.key_blob(b"ssh-rsa", b"\x00\x01\x00\x01", b"\x7f" * 256),
            "ssh-rsa " + self.key_blob(b"ssh-rsa", b"\x01\x00\x01", b"\x00" + b"\x7f" * 256),
            "ecdsa-sha2-nistp256 " + self.key_blob(
                b"ecdsa-sha2-nistp256", b"nistp384", b"\x04" + b"x" * 64
            ),
            "ecdsa-sha2-nistp256 " + self.key_blob(
                b"ecdsa-sha2-nistp256", b"nistp256", b"\x03" + b"x" * 32
            ),
            "sk-ssh-ed25519@openssh.com "
            + self.key_blob(b"sk-ssh-ed25519@openssh.com", b"x" * 32),
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(StateError, "public key is invalid"):
                self.ledger.replace_owner_key(bad, now_ms=NOW)
        self.assertEqual(self.state.owner_key_report(), before)

    def test_every_allowed_key_type_is_accepted(self) -> None:
        for key_type, bits in (("rsa", "3072"), ("ecdsa", "256")):
            directory = Path(self.temporary.name) / f"real-{key_type}"
            directory.mkdir()
            subprocess.run(
                ["ssh-keygen", "-q", "-t", key_type, "-b", bits, "-N", "", "-f", str(directory / "k")],
                check=True,
            )
            line = self.state._owner_signer_content((directory / "k.pub").read_text())
            self.assertTrue(OWNER_SIGNER_LINE.fullmatch(line))
        for key_type, parts in (
            ("sk-ssh-ed25519@openssh.com", (b"x" * 32, b"ssh:")),
            ("sk-ecdsa-sha2-nistp256@openssh.com", (b"nistp256", b"\x04" + b"x" * 64, b"ssh:")),
        ):
            self.state._owner_signer_content(
                f"{key_type} {self.key_blob(key_type.encode('ascii'), *parts)}"
            )

    def test_a_replaced_rsa_key_cannot_come_back_in_another_encoding(self) -> None:
        directory = Path(self.temporary.name) / "rsa"
        directory.mkdir()
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "rsa", "-b", "3072", "-N", "", "-f", str(directory / "k")],
            check=True,
        )
        rsa = (directory / "k.pub").read_text()
        self.ledger.replace_owner_key(rsa, now_ms=NOW)
        self.ledger.replace_owner_key(self.other_key()[1], now_ms=NOW + 1)
        with self.assertRaisesRegex(LedgerError, "replaced earlier"):
            self.ledger.replace_owner_key(rsa, now_ms=NOW + 2)
        fields = rsa.split()
        parts = [
            bytes(part) for part in _ssh_key_parts(base64.b64decode(fields[1]))
        ]
        padded = self.key_blob(parts[0], parts[1], b"\x00" + parts[2])
        with self.assertRaisesRegex(StateError, "public key is invalid"):
            self.ledger.replace_owner_key(f"ssh-rsa {padded}", now_ms=NOW + 2)
        # A history row spelled the padded way, as an older version could have enrolled, still counts.
        self.assertEqual(
            _owner_key_identity(f"codex-agents-owner ssh-rsa {padded}\n".encode("ascii")),
            _owner_key_identity(f"codex-agents-owner ssh-rsa {fields[1]}\n".encode("ascii")),
        )

    def test_damaged_signer_files_do_not_block_opening_or_replacing(self) -> None:
        for name, damage in (
            ("unreadable", lambda path: path.chmod(0o000)),
            ("directory", lambda path: (path.unlink(), path.mkdir())),
            ("fifo", lambda path: (path.unlink(), os.mkfifo(path))),
        ):
            with self.subTest(name):
                base = Path(self.temporary.name) / f"damaged-{name}"
                base.mkdir()
                state = LearningState(base / "state")
                state.allowed_signers_path.write_bytes(b"two\nlines\n")
                damage(state.allowed_signers_path)
                fresh = LearningLedger(state, client_capabilities=self.clients)
                try:
                    report = fresh.replace_owner_key(self.other_key(f"key-{name}")[1], now_ms=NOW)
                    self.assertEqual((report["state"], report["replacements"]), ("ok", 1))
                finally:
                    fresh.close()

    def test_verification_uses_the_checked_bytes_not_the_file(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        old_line = self.state.allowed_signers_path.read_bytes()
        _, public = self.other_key()
        self.ledger.replace_owner_key(public, now_ms=NOW)
        authority = OwnerGrantAuthority.for_state(self.state)
        self.state.allowed_signers_path.write_bytes(old_line)  # after the check
        with self.ledger._lock:
            row = self.ledger._connection.execute(
                "SELECT subject_record_id,signature FROM owner_attestations WHERE project_id=?",
                (self.project_id,),
            ).fetchone()
            content = self.ledger._connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_id=?",
                (self.project_id, row["subject_record_id"]),
            ).fetchone()
        self.assertFalse(authority.verify_owner_record(
            self.project_id, json.loads(bytes(content["content"])), bytes(row["signature"])
        ))

    def test_a_damaged_signer_file_with_no_record_can_be_replaced_or_enrolled(self) -> None:
        base = Path(self.temporary.name) / "fresh"
        base.mkdir()
        state = LearningState(base / "state")
        state.allowed_signers_path.write_bytes(b"two\nlines\n")
        fresh = LearningLedger(state, client_capabilities=self.clients)
        try:
            self.assertEqual(state.owner_key_report()["state"], "mismatch")
            report = fresh.replace_owner_key(self.other_key()[1], now_ms=NOW)
            self.assertEqual((report["state"], report["replacements"]), ("ok", 1))
        finally:
            fresh.close()
        state.allowed_signers_path.unlink()
        self.assertEqual(state.owner_key_report()["state"], "mismatch")  # a key is on record
        base = Path(self.temporary.name) / "deleted"
        base.mkdir()
        untouched = LearningState(base / "state")
        untouched.allowed_signers_path.unlink()
        self.assertEqual(untouched.owner_key_report()["state"], "not_configured")

    def test_the_key_history_only_grows_and_needs_a_first_key(self) -> None:
        for statement in (
            "DELETE FROM owner_key_history",
            "UPDATE owner_key_history SET reason='replaced'",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.ledger._connection.execute(statement)
        base = Path(self.temporary.name) / "fresh"
        base.mkdir()
        fresh_state = LearningState(base / "state")
        fresh = LearningLedger(fresh_state, client_capabilities=self.clients)
        try:
            self.assertEqual(fresh_state.owner_key_report()["state"], "not_configured")
            with self.assertRaisesRegex(LedgerError, "nothing to replace"):
                fresh.replace_owner_key(self.other_key()[1], now_ms=NOW)
        finally:
            fresh.close()

    # S3b: client version, Codex plan, the run-count backstop, restrictive
    # rate-limit events.

    def version(self, provider: str, text: str) -> None:
        (self.tools / f"{provider}-version.txt").write_text(text)

    def limit(self, provider: str, **settings) -> None:
        policy = deepcopy(load_policy(ORCHESTRATION_ROOT / "config" / "routing-policy.json"))
        policy["providers"][provider].update(settings)
        self.ledger._admission_policy = policy

    def test_client_version_must_be_inside_the_reviewed_range(self) -> None:
        identity = self.observe()[0]
        self.assertEqual("0.164.0", identity["client_version"])
        self.attest("codex", identity["account_fingerprint"])
        for text, admitted in (
            ("codex-cli 0.164.0\n", True),   # the lower boundary is inside
            ("codex-cli 0.164.1\n", False),  # the upper boundary is outside
            ("codex-cli 0.164.99\n", False),
            ("codex-cli 0.165.0\n", False),
            ("codex-cli 0.163.9\n", False),
            ("codex-cli 1.0.0\n", False),
            ("", False),                       # missing
            ("codex-cli\n", False),
            ("codex-cli 0.164.0-alpha.1\n", False),
            ("codex-cli 0.164\n", False),
            ("codex-cli 0.164.0 (extra)\n", False),
            ("0.164.0\n", False),
            ("\xff\xfe garbage", False),
        ):
            with self.subTest(text=text):
                self.version("codex", text)
                observed = self.observe()[0]
                if admitted:
                    self.assertEqual("pass", self.admit()["decision"])
                else:
                    self.assert_denied("client_version_unreviewed")
                    if observed["client_version"] is not None:
                        self.assertNotEqual("0.164.0", observed["client_version"])
        claude = self.observe("claude")[0]
        self.attest("claude", claude["account_fingerprint"])
        for text, admitted in (
            ("2.1.0 (Claude Code)\n", True),
            ("2.1.123 (Claude Code)\n", True),
            ("2.2.0 (Claude Code)\n", False),
            ("2.0.99 (Claude Code)\n", False),
            ("Claude Code\n", False),
            ("", False),
        ):
            with self.subTest(text=text):
                self.version("claude", text)
                self.observe("claude")
                if admitted:
                    self.assertEqual("pass", self.admit("claude")["decision"])
                else:
                    self.assert_denied("client_version_unreviewed", "claude")

    def test_client_version_parsing_keeps_only_a_plain_version(self) -> None:
        self.assertEqual("2.1.123", parse_client_version("claude", b"2.1.123 (Claude Code)\n"))
        self.assertEqual("2.1.123", parse_client_version("claude", "2.1.123"))
        self.assertEqual("0.164.0", parse_client_version("codex", b"codex-cli 0.164.0\n"))
        for provider, raw in (
            ("codex", b"0.164.0"), ("claude", b"codex-cli 0.164.0"),
            ("claude", b"2.1.123 (Claude Code)\n2.1.124"), ("claude", b"\xff"),
            ("claude", b"9" * 300), ("claude", b"02.1.0"), ("gemini", b"1.0.0"),
            ("claude", None), ("codex", b"codex-cli 0.164.0 fixture-account-5c2e08"),
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_client_version(provider, raw))
        with self.assertRaisesRegex(ContractError, "plain semantic version"):
            validate_learning_record({
                **self.observe()[0], "client_version": "0.164.0 fixture-account-5c2e08",
            })

    def test_admission_without_usable_settings_is_denied(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.ledger._admission_policy = {"providers": {}}
        self.assert_denied("admission_policy_unavailable")
        self.limit("codex", client_versions={"minimum": "1.0.0", "maximum_exclusive": "0.1.0"})
        self.assert_denied("admission_policy_unavailable")
        self.ledger._admission_policy = None
        self.assertEqual("pass", self.admit()["decision"])

    def test_codex_requires_a_subscription_plan(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        for plan in (
            "free", "fixture-account-5c2e08", "unknown", None,
            "self_serve_business_usage_based", "enterprise_cbp_usage_based",
        ):
            with self.subTest(plan=plan):
                self.observe(**{"rateLimits.planType": plan})
                self.assert_denied("codex_plan_not_subscription")
        result = json.loads(provider_fixture("codex-rate-limits.json"))
        del result["rateLimits"]["planType"]
        (self.tools / "codex-result.json").write_text(json.dumps(result))
        with trusted_tools(self.tools), mock.patch.object(
            self.ledger, "_clock_ms", return_value=self.tick()
        ):
            absent = self.ledger.observe_provider(
                self.project_id, "codex", project_root=self.tools, environ=CLEAN
            )[0]
        self.assertIsNone(absent["plan_type"])
        self.assert_denied("codex_plan_not_subscription")
        for plan in ("plus", "pro", "team", "business", "enterprise"):
            with self.subTest(plan=plan):
                self.observe(**{"rateLimits.planType": plan})
                self.assertEqual("pass", self.admit()["decision"])

    def test_run_cap_counts_admissions_in_the_current_window(self) -> None:
        self.limit("codex", max_admissions_per_window=2)
        resets = self.clock + 3_600_000
        reset_at = {"rateLimits.secondary.resetsAt": resets // 1000}
        identity = self.observe(**reset_at)[0]
        fingerprint = identity["account_fingerprint"]
        self.attest("codex", fingerprint)
        self.open_run()   # under the cap
        self.open_run()   # reaches the cap
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run()
        self.assertEqual("window_run_cap_reached", raised.exception.reason_code)
        with self.ledger.repository(self.project_id) as repository:
            stored = repository.records("admission_decision")
        self.assertEqual(
            ("deny", "window_run_cap_reached", None),
            (stored[-1]["decision"], stored[-1]["reason_code"], stored[-1]["run_id"]),
        )
        self.assertEqual(2, len([item for item in stored if item["decision"] == "pass"]))
        # Over the cap stays denied; a denial is not an admission and never counts.
        self.assert_denied("window_run_cap_reached")
        # Another account's admissions are its own.
        other = self.observe(accountId="fixture-account-other", **reset_at)[0]
        self.assertNotEqual(fingerprint, other["account_fingerprint"])
        self.attest("codex", other["account_fingerprint"])
        self.assertEqual("pass", self.ledger.host_run_admission(
            self.project_id, self.open_run()["run_handle"]
        )["decision"])
        # The window resets: the next window starts five hours before its reset.
        self.clock = resets + 1
        self.observe(**{"rateLimits.secondary.resetsAt": (self.clock + 5 * 3_600_000) // 1000})
        self.assertEqual("pass", self.ledger.host_run_admission(
            self.project_id, self.open_run()["run_handle"]
        )["decision"])
        self.limit("codex", max_admissions_per_window=None)
        self.open_run()
        self.open_run()

    def test_run_cap_window_falls_back_to_the_last_five_hours(self) -> None:
        identity = parse_codex(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=KEY, observed_at_ms=NOW, client_version="0.164.0",
        )
        fingerprint = identity[0]["account_fingerprint"]
        attestation = {"provider": "codex", "account_fingerprint": fingerprint,
                       "observed_at_ms": 1}
        five_hours = 5 * 60 * 60 * 1000

        def decide(prior, headroom=()):
            return evaluate_admission(
                provider="codex",
                identities=[(identity[0], DIGEST_A)],
                attestations=[(attestation, DIGEST_B)],
                billing=[(identity[2], DIGEST_C)], headroom=list(headroom),
                prior_claude_admissions=set(), environ={}, now_ms=NOW + 1,
                admission_policy={
                    "client_versions": {"minimum": "0.164.0", "maximum_exclusive": "0.164.1"},
                    "max_admissions_per_window": 2,
                },
                prior_run_admissions=prior,
                window_headroom=list(headroom),
            )["reason_code"]

        now = NOW + 1
        inside, edge = now - five_hours + 1, now - five_hours
        self.assertEqual("admitted", decide([(fingerprint, inside)]))
        self.assertEqual("window_run_cap_reached", decide([(fingerprint, inside)] * 2))
        self.assertEqual("window_run_cap_reached", decide([(fingerprint, inside)] * 3))
        self.assertEqual("admitted", decide([(fingerprint, edge)] * 2))
        self.assertEqual("admitted", decide([(DIGEST_C, inside)] * 2))
        self.assertEqual("window_run_cap_reached", decide([(fingerprint, now + 1)] * 2))
        # The fixture's five-hour reset is years away, so it is not trusted.
        self.assertEqual(
            (now - five_hours + 1, None),
            window_start_ms("codex", fingerprint, [(identity[1], DIGEST_C)], now),
        )
        soon = {**identity[1], "windows": [
            {**identity[1]["windows"][0], "resets_at": now + 60_000},
        ]}
        self.assertEqual(
            (now + 60_000 - five_hours, DIGEST_C),
            window_start_ms("codex", fingerprint, [(soon, DIGEST_C)], now),
        )
        self.assertEqual(
            "window_run_cap_reached",
            decide([(fingerprint, now + 60_000 - five_hours)] * 2, [(soon, DIGEST_C)]),
        )
        self.assertEqual(
            "admitted",
            decide([(fingerprint, now + 60_000 - five_hours - 1)] * 2, [(soon, DIGEST_C)]),
        )

    def test_many_headroom_records_keep_the_decision_bounded(self) -> None:
        from codex_agents.providers import admission_record

        records = parse_codex(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=KEY, observed_at_ms=NOW, client_version="0.164.0",
        )
        identity, headroom, billing = records
        attestation = {"provider": "codex", "account_fingerprint": identity["account_fingerprint"],
                       "observed_at_ms": 1}
        many = [
            ({**headroom, "observed_at_ms": NOW - index}, f"{index:064x}")
            for index in range(200)
        ]
        exhausted = {**headroom, "windows": [
            {**headroom["windows"][0], "used_percent": 95.0, "resets_at": NOW + 60_000},
        ]}

        def decide(items):
            return evaluate_admission(
                provider="codex", identities=[(identity, DIGEST_A)],
                attestations=[(attestation, DIGEST_B)], billing=[(billing, DIGEST_C)],
                headroom=items, prior_claude_admissions=set(), environ={},
                now_ms=NOW + 1, **CODEX_LIMITS,
            )

        passed = decide(many)
        self.assertEqual("admitted", passed["reason_code"])
        self.assertIn(many[-1][1], passed["evidence_digests"])
        self.assertLessEqual(len(passed["evidence_digests"]), 5)
        admission_record(passed, "run-1")
        denied = decide([*many[:100], (exhausted, "e" * 64), *many[100:]])
        self.assertEqual("provider_exhausted", denied["reason_code"])
        self.assertIn("e" * 64, denied["evidence_digests"])
        admission_record(denied, None)

    def test_restrictive_evidence_stamped_after_now_still_counts(self) -> None:
        identity, headroom, billing = parse_codex(
            json.loads(provider_fixture("codex-rate-limits.json")),
            digest_key=KEY, observed_at_ms=NOW, client_version="0.164.0",
        )
        attestation = {"provider": "codex", "account_fingerprint": identity["account_fingerprint"],
                       "observed_at_ms": 1}
        ahead = NOW + 60_000

        def decide(more_billing=(), more_headroom=()):
            return evaluate_admission(
                provider="codex", identities=[(identity, DIGEST_A)],
                attestations=[(attestation, DIGEST_B)],
                billing=[(billing, DIGEST_C), *more_billing],
                headroom=[(headroom, DIGEST_C), *more_headroom],
                prior_claude_admissions=set(), environ={}, now_ms=NOW + 1, **CODEX_LIMITS,
            )["reason_code"]

        self.assertEqual("admitted", decide())
        paid = {**billing, "paid_usage_usable": True, "observed_at_ms": ahead}
        self.assertEqual("paid_usage_usable", decide(more_billing=[(paid, "a" * 64)]))
        blocked = {**billing, "ordinary_usage_allowed": False, "observed_at_ms": ahead}
        self.assertEqual("provider_exhausted", decide(more_billing=[(blocked, "c" * 64)]))
        full = {**headroom, "observed_at_ms": ahead, "windows": [
            {**headroom["windows"][0], "used_percent": 95.0, "resets_at": ahead + 60_000},
        ]}
        self.assertEqual("provider_exhausted", decide(more_headroom=[(full, "b" * 64)]))

    def test_billing_probe_admissions_do_not_count_toward_the_cap(self) -> None:
        self.limit("claude", max_admissions_per_window=2)
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        first = self.open_run("claude")
        self.event(first, **json.loads(provider_fixture("claude-rate-limit-event.json")))
        with self.ledger._lock, mock.patch.object(
            self.ledger, "_clock_ms", return_value=self.tick()
        ):
            probe = self.ledger._evaluate_admission_locked(
                self.project_id, provider="claude", environ=CLEAN, timestamp=self.clock
            )
            self.assertEqual("pass", probe["decision"])
            for _ in range(3):
                self.ledger._store_probe_admission_locked(
                    self.project_id, {**probe, "observed_at_ms": self.tick()},
                    timestamp=self.clock,
                )
        self.open_run("claude")
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run("claude")
        self.assertEqual("window_run_cap_reached", raised.exception.reason_code)

    def test_a_run_keeps_every_more_restrictive_rate_limit_event(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        quiet = {"isUsingOverage": False, "overageStatus": "rejected",
                 "overageDisabledReason": "org_level_disabled"}
        later = (self.clock + 3_600_000) // 1000

        def kept(**info):
            return [record["record_type"] for record in self.event(run, **info)]

        base = {"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.2,
                "resetsAt": later, **quiet}
        self.assertEqual(["provider_headroom", "provider_billing"], kept(**base))
        self.assertEqual([], kept(**base))
        self.assertEqual([], kept(**{**base, "utilization": 0.1}))
        self.assertEqual(["provider_headroom"], kept(**{**base, "utilization": 0.5}))
        self.assertEqual(["provider_headroom"], kept(**{**base, "status": "allowed_warning"}))
        self.assertEqual(["provider_headroom"], kept(**{**base, "rateLimitType": "seven_day"}))
        self.assertEqual(
            ["provider_billing"], kept(**{**base, "status": "allowed_warning",
                                         "overageDisabledReason": None}),
        )
        # Once billing is unknown, a favourable event cannot restore it.
        self.assertEqual([], kept(**{**base, "status": "allowed_warning"}))
        self.assert_denied("billing_unverified", "claude")
        with self.ledger.repository(self.project_id) as repository:
            headroom = [record for record in repository.records("provider_headroom")
                        if record["run_id"] is not None]
        self.assertEqual({"during"}, {record["phase"] for record in headroom})
        self.assertEqual(4, len(headroom))

    def test_a_run_cannot_record_rate_limit_events_without_bound(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        quiet = {"isUsingOverage": False, "overageStatus": "rejected",
                 "overageDisabledReason": "org_level_disabled"}
        first_reset = (self.clock + 3_600_000) // 1000
        later_reset = (self.clock + 7_200_000) // 1000
        recorded = len(self.event(run, status="rejected", rateLimitType="five_hour",
                                  utilization=1.0, resetsAt=first_reset, **quiet))
        # Allowed readings that run longer than the rejection keep arriving
        # until the run reaches the cap.
        with self.assertRaisesRegex(ContractError, "too many rate-limit events"):
            for step in range(200):
                recorded += len(self.event(
                    run, status="allowed", rateLimitType="five_hour",
                    utilization=step / 1000, resetsAt=later_reset, **quiet,
                ))
        with self.ledger.repository(self.project_id) as repository:
            stored = [
                record for kind in ("provider_headroom", "provider_billing")
                for record in repository.records(kind) if record["phase"] == "during"
            ]
            merged = [record for record in repository.records("provider_headroom")
                      if record["phase"] == "during"][-1]
        self.assertEqual(63, recorded)
        self.assertEqual(64, len(stored))
        # At the cap the run's whole restriction is merged: the highest status
        # with the latest reset.
        self.assertEqual(
            ("rejected", 100.0, later_reset * 1000),
            (merged["status"], merged["windows"][0]["used_percent"],
             merged["windows"][0]["resets_at"]),
        )
        # The run is fenced: later events are refused and nothing is stored.
        with self.assertRaisesRegex(ContractError, "too many rate-limit events"):
            self.event(run, status="rejected", rateLimitType="five_hour",
                       utilization=1.0, resetsAt=later_reset + 60, **quiet)
        with self.ledger.repository(self.project_id) as repository:
            self.assertEqual(64, len([
                record for kind in ("provider_headroom", "provider_billing")
                for record in repository.records(kind) if record["phase"] == "during"
            ]))
        # After the earlier reset the merged restriction still applies...
        after_first = first_reset * 1000 + 1
        self.clean_claude_billing(after_first)
        self.assert_denied("provider_exhausted", "claude", now_ms=after_first + 1)
        # ...and it lifts at the later reset.
        after_later = later_reset * 1000 + 1
        self.clean_claude_billing(after_later)
        self.assertEqual("pass", self.admit("claude", now_ms=after_later + 1)["decision"])

    def test_a_rejection_is_not_covered_by_a_longer_allowed_reading(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        quiet = {"isUsingOverage": False, "overageStatus": "rejected",
                 "overageDisabledReason": "org_level_disabled"}
        first_reset = (self.clock + 3_600_000) // 1000
        later_reset = (self.clock + 7_200_000) // 1000
        self.event(run, status="rejected", rateLimitType="five_hour", utilization=1.0,
                   resetsAt=first_reset, **quiet)
        self.event(run, status="allowed", rateLimitType="five_hour", utilization=0.5,
                   resetsAt=later_reset, **quiet)
        kept = self.event(run, status="rejected", rateLimitType="five_hour",
                          utilization=1.0, resetsAt=later_reset, **quiet)
        self.assertEqual(["provider_headroom"], [item["record_type"] for item in kept])
        after_first = first_reset * 1000 + 1
        self.clean_claude_billing(after_first)
        self.assert_denied("provider_exhausted", "claude", now_ms=after_first + 1)

    def test_a_later_reset_reading_cannot_free_counted_admissions(self) -> None:
        self.limit("codex", max_admissions_per_window=1)
        first = {"rateLimits.secondary.resetsAt": (self.clock + 3_600_000) // 1000}
        identity = self.observe(**first)[0]
        self.attest("codex", identity["account_fingerprint"])
        self.open_run()
        # A later reading reports a reset five hours away, which alone would
        # start the window now; the earlier window is still live, so it counts.
        self.observe(**{"rateLimits.secondary.resetsAt": (self.clock + 5 * 3_600_000) // 1000})
        self.assert_denied("window_run_cap_reached")
        with self.assertRaises(AdmissionDenied) as raised:
            self.open_run()
        self.assertEqual("window_run_cap_reached", raised.exception.reason_code)
        headroom = [
            ({"provider": "codex", "account_fingerprint": DIGEST_A, "observed_at_ms": NOW,
              "windows": [{"window_minutes": 300, "scope": None, "used_percent": 1.0,
                           "resets_at": reset}]}, digest)
            for reset, digest in ((NOW + 3_600_000, DIGEST_B), (NOW + 5 * 3_600_000, DIGEST_C))
        ]
        self.assertEqual(
            (NOW + 3_600_000 - 5 * 3_600_000, DIGEST_B),
            window_start_ms("codex", DIGEST_A, headroom, NOW),
        )
        # Once the earlier window resets, the later reading places the window.
        self.assertEqual(
            (NOW, DIGEST_C), window_start_ms("codex", DIGEST_A, headroom, NOW + 3_600_000 + 1)
        )

    def test_the_run_cap_counts_the_account_across_projects(self) -> None:
        self.limit("codex", max_admissions_per_window=1)
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        self.open_run()
        other_root = Path(self.temporary.name) / "other-project"
        other_root.mkdir()
        first_project = self.project_id
        self.project_id = self.ledger.register_project(other_root, now_ms=1)
        try:
            other = self.observe()[0]
            self.assertEqual(identity["account_fingerprint"], other["account_fingerprint"])
            self.attest("codex", other["account_fingerprint"])
            with self.assertRaises(AdmissionDenied) as raised:
                self.open_run()
            self.assertEqual("window_run_cap_reached", raised.exception.reason_code)
        finally:
            self.project_id = first_project

    def test_the_cap_window_is_placed_from_every_projects_readings(self) -> None:
        self.limit("codex", max_admissions_per_window=1)
        identity = self.observe(
            **{"rateLimits.secondary.resetsAt": (self.clock + 3_600_000) // 1000}
        )[0]
        self.attest("codex", identity["account_fingerprint"])
        self.open_run()
        other_root = Path(self.temporary.name) / "other-project"
        other_root.mkdir()
        first_project = self.project_id
        self.project_id = self.ledger.register_project(other_root, now_ms=1)
        try:
            self.clock += 10_000
            # Project B alone sees a reset five hours away, which would start
            # its window after project A's admission.
            other = self.observe(
                **{"rateLimits.secondary.resetsAt": (self.clock + 5 * 3_600_000) // 1000}
            )[0]
            self.attest("codex", other["account_fingerprint"])
            with self.assertRaises(AdmissionDenied) as raised:
                self.open_run()
            self.assertEqual("window_run_cap_reached", raised.exception.reason_code)
            # The denial is stored in this project, citing only its own records
            # (the ledger refuses evidence from another project).
            with self.ledger.repository(self.project_id) as repository:
                stored = repository.records("admission_decision")[-1]
            self.assertEqual(
                ("deny", "window_run_cap_reached"), (stored["decision"], stored["reason_code"])
            )
        finally:
            self.project_id = first_project

    def test_a_reset_less_rejection_survives_the_overflow_merge(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        quiet = {"isUsingOverage": False, "overageStatus": "rejected",
                 "overageDisabledReason": "org_level_disabled"}
        reset = (self.clock + 3_600_000) // 1000
        recorded = 0
        for step in range(62):
            recorded += len(self.event(
                run, status="allowed", rateLimitType="five_hour",
                utilization=step / 1000, resetsAt=reset, **quiet,
            ))
        self.assertEqual(63, recorded)
        rejected_at = self.clock + 1
        with self.assertRaisesRegex(ContractError, "too many rate-limit events"):
            self.event(run, status="rejected", rateLimitType="five_hour",
                       utilization=1.0, **quiet)
        with self.ledger.repository(self.project_id) as repository:
            merged = [record for record in repository.records("provider_headroom")
                      if record["phase"] == "during"][-1]
        self.assertEqual("rejected", merged["status"])
        # The reset-less rejection lasts its day, not the allowed readings' hour.
        self.assertEqual(rejected_at + 24 * 3_600_000, merged["windows"][0]["resets_at"])
        after_reset = reset * 1000 + 1
        self.clean_claude_billing(after_reset)
        self.assert_denied("provider_exhausted", "claude", now_ms=after_reset + 1)

    @contextlib.contextmanager
    def other_project(self):
        """Run the block in a second project of the same ledger and account."""

        root = Path(self.temporary.name) / f"project-{self.tick()}"
        root.mkdir()
        first = self.project_id
        self.project_id = self.ledger.register_project(root, now_ms=1)
        try:
            fingerprint = self.observe("claude")[0]["account_fingerprint"]
            self.attest("claude", fingerprint)
            yield
        finally:
            self.project_id = first

    def test_overage_in_one_project_denies_another(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        self.event(run, status="allowed", rateLimitType="five_hour", utilization=0.1,
                   isUsingOverage=True)
        with self.other_project():
            self.assert_denied("paid_usage_usable", "claude")
            with self.ledger.repository(self.project_id) as repository:
                cited = repository.records("admission_decision")[-1]["evidence_digests"]
            with self.ledger._lock:
                rows = self.ledger._connection.execute(
                    "SELECT content_digest,record_type FROM records WHERE project_id=?",
                    (self.project_id,),
                ).fetchall()
            own = {str(row[0]) for row in rows}
            billing_here = {str(row[0]) for row in rows if row[1] == "provider_billing"}
            # The other project's billing decides, but only this project's
            # records are cited, and this project holds no billing at all.
            self.assertTrue(cited and set(cited) <= own)
            self.assertEqual(set(), billing_here)

    def test_the_bootstrap_is_once_per_account(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        stored = self.ledger.host_run_admission(
            self.project_id, self.open_run("claude")["run_handle"]
        )
        self.assertEqual("bootstrap", stored["admission_basis"])
        with self.other_project():
            with self.assertRaises(AdmissionDenied) as raised:
                self.open_run("claude")
            self.assertEqual("billing_unverified", raised.exception.reason_code)

    def test_a_rejection_in_one_project_blocks_another_until_reset(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        reset = (self.clock + 3_600_000) // 1000
        self.event(run, status="rejected", rateLimitType="five_hour", utilization=1.0,
                   resetsAt=reset, isUsingOverage=False, overageStatus="rejected",
                   overageDisabledReason="org_level_disabled")
        with self.other_project():
            self.assert_denied("provider_exhausted", "claude")
            after = reset * 1000 + 1
            self.clean_claude_billing(after)
            self.assertEqual("pass", self.admit("claude", now_ms=after + 1)["decision"])

    def test_claude_billing_must_be_fresh_except_for_the_bootstrap(self) -> None:
        fingerprint = self.observe("claude")[0]["account_fingerprint"]
        self.attest("claude", fingerprint)
        run = self.open_run("claude")
        self.event(run, **json.loads(provider_fixture("claude-rate-limit-event.json")))
        self.assertEqual("pass", self.admit("claude")["decision"])
        day_later = self.clock + 24 * 60 * 60 * 1000
        self.observe("claude", now_ms=day_later)
        self.assert_denied("billing_unverified", "claude", now_ms=day_later + 1)
        # A probe is still admitted on that older clean evidence, to refresh it.
        with self.ledger._lock:
            probe = self.ledger._evaluate_admission_locked(
                self.project_id, provider="claude", environ=CLEAN,
                timestamp=day_later + 2, probe=True,
            )
        self.assertEqual("pass", probe["decision"])
        self.clean_claude_billing(day_later + 3)
        self.assertEqual("pass", self.admit("claude", now_ms=day_later + 4)["decision"])

    def test_future_resetless_exhaustion_cannot_be_erased_by_project_deletion(self) -> None:
        for index, (provider, empty) in enumerate((('codex', False), ('claude', False), ('claude', True))):
            with self.subTest(provider=provider, empty_unified_windows=empty):
                root = Path(self.temporary.name) / f'future-restriction-{index}'
                root.mkdir()
                held = self.ledger.register_project(root, now_ms=1)
                observed = self.clock + 1
                if empty:
                    headroom = providers.claude_rate_limit_records({
                        'status': 'rejected', 'unifiedWindows': {}, 'isUsingOverage': False,
                        'overageStatus': 'rejected', 'overageDisabledReason': 'org_level_disabled',
                    }, account_fingerprint='a' * 64, observed_at_ms=observed)[0]
                else:
                    headroom = providers._record('provider_headroom', {
                        'provider': provider, 'account_fingerprint': 'a' * 64, 'observed_at_ms': observed,
                        'status': 'rejected' if provider == 'claude' else None,
                        'run_id': None, 'phase': None,
                        'windows': [{'scope': 'five_hour' if provider == 'claude' else None,
                                     'window_minutes': 300, 'used_percent': 95 if provider == 'codex' else None,
                                     'resets_at': None}],
                    })
                # This project has no admitted run, probe or paid billing record
                # that could mask the headroom guard with a different denial.
                self.ledger._store_provider_observations(held, [headroom], timestamp=observed)
                with mock.patch.object(self.ledger, '_clock_ms', return_value=self.clock):
                    with self.assertRaisesRegex(LedgerError, 'project_holds_account_restrictions'):
                        self.ledger.delete_project(held, now_ms=observed + 30 * 24 * 3600000)
                with self.ledger.repository(held) as repository:
                    self.assertEqual(1, len(repository.records('provider_headroom')))
                with mock.patch.object(self.ledger, '_clock_ms', return_value=observed + 24 * 3600000 + 1):
                    self.assertTrue(self.ledger.delete_project(held).startswith('del_'))

    def test_a_project_holding_account_restrictions_cannot_be_deleted(self) -> None:
        with self.other_project():
            run = self.open_run("claude")
            self.event(run, status="allowed", rateLimitType="five_hour", utilization=0.1,
                       isUsingOverage=True)
            held = self.project_id
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
            with self.assertRaisesRegex(LedgerError, "project_holds_account_restrictions"):
                self.ledger.delete_project(held)
            # A caller's time cannot age the restriction out; only the ledger clock.
            with self.assertRaisesRegex(LedgerError, "project_holds_account_restrictions"):
                self.ledger.delete_project(held, now_ms=self.clock + 10 * 24 * 3_600_000)
        with mock.patch.object(
            self.ledger, "_clock_ms", return_value=self.clock + 24 * 3_600_000 + 1
        ):
            self.assertTrue(self.ledger.delete_project(held).startswith("del_"))

    def test_spend_control_denial_survives_deleting_the_recording_project(self) -> None:
        identity = self.observe()[0]
        fingerprint = identity["account_fingerprint"]
        self.credits_attest(fingerprint)
        first = self.project_id
        # Another project of the same account records spend control reached.
        with self.other_project():
            self.attest("codex", fingerprint)
            self.credits_attest(fingerprint, issued_at=2)
            self.observe(**{
                "rateLimits.credits.hasCredits": True,
                "rateLimits.spendControlReached": True,
            })
            held = self.project_id
            with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
                with self.assertRaisesRegex(LedgerError, "project_holds_account_restrictions"):
                    self.ledger.delete_project(held)
        self.assertEqual(first, self.project_id)
        self.observe(**{"rateLimits.credits.hasCredits": True})
        self.assert_denied("codex_spend_control_reached")
        # A reading stamped in the future counts too.
        with self.other_project():
            future = self.clock + 3_600_000
            self.observe(now_ms=future, **{"rateLimits.spendControlReached": True})
            with mock.patch.object(self.ledger, "_clock_ms", return_value=self.clock + 1):
                with self.assertRaisesRegex(LedgerError, "project_holds_account_restrictions"):
                    self.ledger.delete_project(self.project_id)

    def test_billing_records_without_spend_control_verify_in_a_backup(self) -> None:
        identity = self.observe()[0]
        legacy = self.observe()[2]
        del legacy["spend_control_reached"]
        legacy = {**legacy, "record_id": "provider_billing:legacy"}
        self.assertNotIn("spend_control_reached", validate_learning_record(legacy))
        self.ledger._store_provider_observations(
            self.project_id, [legacy], timestamp=self.tick()
        )
        self.assertEqual(identity["provider"], "codex")
        self.ledger.create_sanitized_backup(
            self.project_id, file_id="legacy_billing",
            expires_at_ms=time.time_ns() // 1_000_000 + 60_000,
        )
        self.ledger.verify_sanitized_backup(self.project_id, file_id="legacy_billing")
        with self.ledger.repository(self.project_id) as repository:
            stored = [r for r in repository.records("provider_billing")
                      if r["record_id"] == "provider_billing:legacy"]
        self.assertEqual(1, len(stored))
        self.assertNotIn("spend_control_reached", stored[0])
        with self.assertRaises(ContractError):
            validate_learning_record({**legacy, "spend_control_reached": "yes"})

    def test_the_locked_recheck_reads_the_clock_it_holds(self) -> None:
        identity = self.observe()[0]
        self.attest("codex", identity["account_fingerprint"])
        orchestration = self.ledger.open_orchestration(
            self.project_id, client_version="codex-host-v1", now_ms=NOW
        )
        fresh = self.clock + 1
        # The opener passes its first check, then waits for the lock past the
        # point where the identity is too old to admit.
        stale = identity["observed_at_ms"] + IDENTITY_MAX_AGE_MS + 1
        times = iter([fresh, stale])
        with mock.patch.object(self.ledger, "_clock_ms", side_effect=lambda: next(times)), \
                self.assertRaises(AdmissionDenied) as raised:
            self._open_run("codex", orchestration, fresh)
        self.assertEqual("provider_identity_missing", raised.exception.reason_code)
        with self.ledger.repository(self.project_id) as repository:
            stored = repository.records("admission_decision")[-1]
        self.assertEqual(
            ("provider_identity_missing", stale), (stored["reason_code"], stored["observed_at_ms"])
        )
        # A pass is stamped with the time the lock was held, too.
        self.observe()
        later = self.clock + 5
        times = iter([self.clock + 1, later])
        with mock.patch.object(self.ledger, "_clock_ms", side_effect=lambda: next(times)):
            opened = self._open_run("codex", orchestration, self.clock + 1)
        self.assertEqual(
            later,
            self.ledger.host_run_admission(self.project_id, opened["run_handle"])["observed_at_ms"],
        )

    def test_database_never_stores_raw_identity_values(self) -> None:
        for provider in ("codex", "claude"):
            fingerprint = self.observe(provider)[0]["account_fingerprint"]
            self.attest(provider, fingerprint)
            self.admit(provider)
        # Identifiers placed in label fields are dropped too.
        self.observe("claude", subscriptionType="fixture-org-7f3a91")
        self.observe(**{"rateLimits.planType": "fixture-account-5c2e08"})
        self.ledger.close()
        stored = b"".join(
            path.read_bytes()
            for path in Path(self.state.root).rglob("*")
            if path.is_file()
        )
        self.assertIn(b"provider_identity", stored)
        for raw in RAW_IDENTITY:
            self.assertNotIn(raw, stored)
        self.ledger = LearningLedger(self.state, client_capabilities=self.clients)


if __name__ == "__main__":
    unittest.main()
