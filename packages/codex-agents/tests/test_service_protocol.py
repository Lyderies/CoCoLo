from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import ast
import json
import unittest
from dataclasses import FrozenInstanceError, replace

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT

from codex_agents.service_protocol import (
    MAX_CONTROL_REQUEST_BYTES,
    MAX_ATTEMPT,
    MAX_GENERATION,
    REASON_CODES,
    ServiceProtocolError,
    ServiceTarget,
    canonical_request_bytes,
    decode_service_request,
    service_request_digest,
)


def request(**changes) -> dict:
    return {
        "protocol_version": 1, "operation": "project.status", "request_id": "request-1",
        "project_id": "prj_" + "1" * 32, "primary_session_id": "session-1",
        "supervisor_instance_id": "instance-1", "supervisor_generation": 1,
        "runtime_digest": "2" * 64,
        "target": {"orchestration_handle": "orc_" + "3" * 48, "run_handle": None, "attempt": None},
        **changes,
    }


def wire(value: object) -> bytes:
    return json.dumps(value).encode("utf-8")


class ServiceProtocolTests(unittest.TestCase):
    def assert_rejected(self, payload, reason: str) -> None:
        with self.assertRaises(ServiceProtocolError) as caught:
            decode_service_request(payload)
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertEqual((reason,), caught.exception.args)
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_status_and_cancel_targets_round_trip_as_immutable_claims(self) -> None:
        for operation, target in (
            ("project.status", request()["target"]),
            ("project.status", {**request()["target"], "run_handle": "hrn_" + "4" * 48, "attempt": 1}),
            ("project.cancel", {**request()["target"], "run_handle": "hrn_" + "4" * 48, "attempt": 2}),
        ):
            with self.subTest(operation=operation, target=target):
                decoded = decode_service_request(wire(request(operation=operation, target=target)))
                self.assertEqual(decoded, decode_service_request(canonical_request_bytes(decoded)))
                with self.assertRaises(FrozenInstanceError):
                    decoded.primary_session_id = "different-session"
                with self.assertRaises(FrozenInstanceError):
                    decoded.target.attempt = 99

    def test_claimed_authority_sql_and_arbitrary_control_targets_are_rejected(self) -> None:
        for field in ("producer", "role", "sandbox_ok", "no_api_keys", "grant", "pid", "path", "sql"):
            with self.subTest(field=field):
                self.assert_rejected(wire(request(**{field: "CANARY-REJECTED-VALUE"})), "invalid_envelope")
                self.assert_rejected(
                    wire(request(target={**request()["target"], field: "CANARY-REJECTED-VALUE"})),
                    "invalid_target",
                )
        for operation in ("execute", "query", "account.claim", "__getattribute__", [], {}, None):
            with self.subTest(operation=operation):
                self.assert_rejected(wire(request(operation=operation)), "unsupported_operation")

    def test_versions_are_strict_and_envelopes_are_closed(self) -> None:
        for version in (True, False, 0, 2, 1.0, "1", None, [], {}):
            with self.subTest(version=version):
                self.assert_rejected(wire(request(protocol_version=version)), "unsupported_version")
        for field in request():
            missing = request()
            del missing[field]
            with self.subTest(missing=field):
                self.assert_rejected(wire(missing), "invalid_envelope")
        for value in (None, True, 1, [], "CANARY"):
            self.assert_rejected(wire(value), "invalid_envelope")

    def test_exact_target_and_generation_types_and_bounds(self) -> None:
        for value in (0, -1, True, 1.0, "1", None, [], {}, MAX_GENERATION + 1):
            with self.subTest(generation=value):
                self.assert_rejected(wire(request(supervisor_generation=value)), "invalid_envelope")
        self.assertEqual(MAX_GENERATION, decode_service_request(wire(
            request(supervisor_generation=MAX_GENERATION),
        )).supervisor_generation)
        for target in (
            None, [], {}, {**request()["target"], "orchestration_handle": "CANARY/path"},
            {**request()["target"], "run_handle": "hrn_" + "4" * 48},
            {**request()["target"], "attempt": 1},
            *({**request()["target"], "run_handle": "hrn_" + "4" * 48, "attempt": value}
              for value in (0, -1, True, 1.0, "1", [], {}, (1 << 31))),
            {**request()["target"], "run_handle": "CANARY/path", "attempt": 1},
        ):
            with self.subTest(target=target):
                self.assert_rejected(wire(request(target=target)), "invalid_target")
        self.assert_rejected(wire(request(operation="project.cancel")), "invalid_target")

    def test_identifiers_never_accept_paths_unicode_controls_or_unbounded_text(self) -> None:
        for field in ("request_id", "project_id", "primary_session_id", "supervisor_instance_id", "runtime_digest"):
            for value in ("", "../CANARY", "CANARY\n", "CANARY/child", "\ud800", "x" * 161, True, [], {}):
                with self.subTest(field=field, value=value):
                    self.assert_rejected(wire(request(**{field: value})), "invalid_envelope")

    def test_duplicate_fields_are_rejected_at_both_levels(self) -> None:
        normal = wire(request())
        duplicate = b'{"operation":"CANARY",' + normal[1:]
        self.assert_rejected(duplicate, "duplicate_fields")
        nested = normal.replace(b'"attempt": null', b'"attempt":null,"attempt":1')
        self.assert_rejected(nested, "duplicate_fields")
        escaped = b'{"oper\\u0061tion":"CANARY",' + normal[1:]
        self.assert_rejected(escaped, "duplicate_fields")

    def test_transport_bytes_and_parser_failures_have_only_fixed_codes(self) -> None:
        self.assert_rejected("CANARY", "invalid_envelope")
        self.assert_rejected(b"CANARY\xff", "invalid_encoding")
        for payload in (b"CANARY", b"{}\n{}", b"",
                        wire(request()).replace(b'"supervisor_generation": 1', b'"supervisor_generation":NaN')):
            self.assert_rejected(payload, "invalid_json")
        for constant in (b"Infinity", b"-Infinity"):
            self.assert_rejected(wire(request()).replace(b'"supervisor_generation": 1', b'"supervisor_generation":' + constant), "invalid_json")
        class PayloadSubclass(bytes):
            def __len__(self):
                return 1

            def decode(self, *_args, **_options):
                raise RuntimeError("CANARY-DECODE")

        self.assert_rejected(PayloadSubclass(b"CANARY"), "invalid_envelope")
        # Python versions differ in parser recursion limits. Both the parser
        # rejection and rejection of a parsed non-object use fixed safe codes.
        with self.assertRaises(ServiceProtocolError) as caught:
            decode_service_request(b"[" * 10_000 + b"]" * 10_000)
        self.assertIn(caught.exception.reason_code, {"invalid_json", "invalid_envelope"})
        self.assertIsNone(caught.exception.__context__)
        normal = wire(request())
        exact = normal + b" " * (MAX_CONTROL_REQUEST_BYTES - len(normal))
        self.assertEqual("request-1", decode_service_request(exact).request_id)
        self.assert_rejected(exact + b" ", "request_too_large")
        self.assert_rejected(b"\xff" * (MAX_CONTROL_REQUEST_BYTES + 1), "request_too_large")
        self.assert_rejected(b"CANARY" * MAX_CONTROL_REQUEST_BYTES, "request_too_large")

    def test_specific_identifiers_are_stricter_than_generic_opaque_ids(self) -> None:
        cases = {
            "project_id": ("job_" + "1" * 32, "prj_" + "a" * 31, "prj_" + "a" * 33, "prj_" + "A" * 32),
            "runtime_digest": ("digest:" + "2" * 64, "2" * 63, "2" * 65, "A" * 64),
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_rejected(wire(request(**{field: value})), "invalid_envelope")
        for field, values in {
            "orchestration_handle": ("run_" + "3" * 48, "orc_" + "3" * 47, "orc_" + "3" * 49, "orc_" + "A" * 48, [], {}, True),
            "run_handle": ("orc_" + "4" * 48, "hrn_" + "4" * 47, "hrn_" + "4" * 49, "hrn_" + "A" * 48, [], {}, True),
        }.items():
            for value in values:
                target = {**request()["target"], "run_handle": "hrn_" + "4" * 48, "attempt": 1, field: value}
                with self.subTest(field=field, value=value):
                    self.assert_rejected(wire(request(target=target)), "invalid_target")
        decoded = decode_service_request(wire(request(
            request_id="x" * 160, primary_session_id="y" * 160, supervisor_instance_id="z" * 160,
            target={**request()["target"], "run_handle": "hrn_" + "4" * 48, "attempt": MAX_ATTEMPT},
        )))
        self.assertEqual(MAX_ATTEMPT, decoded.target.attempt)
        self.assertEqual(160, len(decoded.request_id))

    def test_canonical_digest_binds_all_scope_and_target_fields(self) -> None:
        normal = request()
        decoded = decode_service_request(wire(normal))
        digest = service_request_digest(decoded)
        reordered = dict(reversed(list(normal.items())))
        self.assertEqual(digest, service_request_digest(decode_service_request(wire(reordered))))
        reordered["target"] = dict(reversed(list(normal["target"].items())))
        self.assertEqual(digest, service_request_digest(decode_service_request(wire(reordered))))
        for field, value in (
            ("request_id", "request-2"), ("project_id", "prj_" + "5" * 32),
            ("primary_session_id", "session-2"), ("supervisor_instance_id", "instance-2"),
            ("supervisor_generation", 2), ("runtime_digest", "6" * 64),
            ("target", {**normal["target"], "orchestration_handle": "orc_" + "7" * 48}),
            ("target", {**normal["target"], "run_handle": "hrn_" + "8" * 48, "attempt": 1}),
        ):
            with self.subTest(field=field):
                changed = decode_service_request(wire(request(**{field: value})))
                self.assertNotEqual(digest, service_request_digest(changed))
        run = decode_service_request(wire(request(target={
            **normal["target"], "run_handle": "hrn_" + "8" * 48, "attempt": 1,
        })))
        self.assertNotEqual(service_request_digest(run), service_request_digest(replace(run, operation="project.cancel")))
        self.assertNotEqual(service_request_digest(run), service_request_digest(replace(run, target=replace(run.target, attempt=2))))
        self.assertNotEqual(
            service_request_digest(run),
            service_request_digest(replace(run, target=replace(run.target, run_handle="hrn_" + "9" * 48))),
        )

    def test_protocol_one_canonical_bytes_and_digest_are_pinned(self) -> None:
        decoded = decode_service_request(wire(request()))
        expected = (
            b'{"operation":"project.status","primary_session_id":"session-1",'
            b'"project_id":"prj_11111111111111111111111111111111","protocol_version":1,'
            b'"request_id":"request-1",'
            b'"runtime_digest":"2222222222222222222222222222222222222222222222222222222222222222",'
            b'"supervisor_generation":1,"supervisor_instance_id":"instance-1",'
            b'"target":{"attempt":null,'
            b'"orchestration_handle":"orc_333333333333333333333333333333333333333333333333",'
            b'"run_handle":null}}'
        )
        self.assertEqual(expected, canonical_request_bytes(decoded))
        self.assertEqual("587ccd38f6347612dd0501fd296e20487bdbfdcf66ca06349efcf7f13e4c2edc", service_request_digest(decoded))

    def test_direct_construction_does_not_bypass_validation(self) -> None:
        decoded = decode_service_request(wire(request()))
        for changes in (
            {"protocol_version": True}, {"operation": "sql"}, {"operation": "project.cancel"},
            {"supervisor_generation": False}, {"target": {}},
        ):
            with self.subTest(changes=changes), self.assertRaises(ServiceProtocolError):
                replace(decoded, **changes)
        with self.assertRaises(ServiceProtocolError):
            ServiceTarget("CANARY", None, None)

        class SpoofedOperation(str):
            def __hash__(self):
                return hash("project.status")

            def __eq__(self, _other):
                return True

        with self.assertRaises(ServiceProtocolError):
            replace(decoded, operation=SpoofedOperation("CANARY"))
        with self.assertRaises(ServiceProtocolError):
            replace(decoded, request_id=SpoofedOperation("CANARY"))
        # A trusted consumer can subvert Python's frozen guard explicitly;
        # canonicalization still validates the snapshot before hashing it.
        object.__setattr__(decoded, "protocol_version", True)
        with self.assertRaises(ServiceProtocolError):
            service_request_digest(decoded)
        with self.assertRaises(ServiceProtocolError) as caught:
            service_request_digest(object.__new__(type(decoded)))
        self.assertIsNone(caught.exception.__context__)
        for reason in ("CANARY", [], {}, None):
            self.assertEqual("invalid_envelope", str(ServiceProtocolError(reason)))
        self.assertEqual(set(REASON_CODES), {str(ServiceProtocolError(code)) for code in REASON_CODES})

    def test_contract_module_has_no_persistence_or_transport_imports(self) -> None:
        # Keep this pure boundary independent of modules that initialize stores,
        # sockets, clients or authority while validating untrusted input.
        tree = ast.parse((ORCHESTRATION_ROOT / "codex_agents/service_protocol.py").read_text())
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0] for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        self.assertEqual({"__future__", "hashlib", "json", "re", "dataclasses", "typing"}, imports)
        self.assertFalse(self._impure_syntax(tree))
        for source in ("from . import store", "__import__('sqlite3')", "open('db')",
                       "importlib.import_module('socket')", "eval('CANARY')"):
            with self.subTest(source=source):
                self.assertTrue(self._impure_syntax(ast.parse(source)))

    @staticmethod
    def _impure_syntax(tree: ast.AST) -> bool:
        # A source lint guard, not a proof of all possible Python side effects.
        forbidden = {"__import__", "open", "eval", "exec", "compile", "globals", "locals"}
        io_methods = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "import_module", "connect", "execute"}
        return any(
            isinstance(node, ast.ImportFrom) and node.level != 0
            or isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name) and node.func.id in forbidden
                or isinstance(node.func, ast.Attribute) and node.func.attr in io_methods
            )
            for node in ast.walk(tree)
        )


if __name__ == "__main__":
    unittest.main()
