"""Shared fixture for the core API tests (TM0, PLAN-0.4.0-terminal.md section 9).

The module under test, `codex_agents.core_api`, is imported inside the fixture, so a module that
fails to import fails each test on its own. The contract these tests fix:

  core_api.CoreService(records_dir, socket_path, *, clock=callable, expected_uid=int, **limits)
      .start() / .stop(); records_dir holds units.jsonl, state.json, events.jsonl, inbox.jsonl,
      commands.jsonl, tokens.json (files stay canonical; the core is a view plus a command gate).
      Limits (keyword): max_line_bytes, max_connections, max_subscriptions, event_buffer, method_seconds.
  core_api.CoreClient(socket_path, token=None)
      .call(method, params=None) -> result dict; raises core_api.ApiError with .code (a string)
      .subscribe(params) -> iterator of notifications; .close()
  core_api.mint_token(records_dir, scope, label="") -> token string (owner's real terminal only); the
      fixtures call the storage step core_api._mint_token(records_dir, scope), which skips that check.
  core_api.in_process(service, token) -> a client-like object with the same .call, no socket; like a
      socket client it has no scope until initialize is given a valid token.
  core_api.PROTOCOL_VERSION = "1.0".
"""
import importlib
import json
import os
import shutil
import tempfile
import time
import unittest

import tests.support as support


def core_api():
    return importlib.import_module("codex_agents.core_api")


def jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def read_jsonl(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


UNITS = [
    {"id": "U-0001", "kind": "child", "state": "running", "title": "Build B1", "model": "sonnet", "effort": "high"},
    {"id": "U-0002", "kind": "command", "state": "done", "title": "Run tests", "exit": 0},
]
STATE = {"phase": "build", "session": "S-7", "ctx": 120000, "window": "unknown", "cost": 1.25,
         "usage": {"input": 10, "output": 5}, "ci": {"status": "green"}, "versions": {"installed": "0.8.40"}}


class CoreCase(unittest.TestCase):
    """A records folder, a short socket path, a fake clock and a started service."""
    start_service = True
    service_options = {}

    def setUp(self):
        from pathlib import Path
        self.dir = Path(tempfile.mkdtemp(dir=support.SHORT_TMP, prefix="core"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.records = self.dir / "records"
        self.records.mkdir()
        self.socket = str(self.dir / "o.sock")
        self.clock = Clock()
        self.mod = core_api()  # fails here, for the right reason, while the module is missing
        self.seed()
        self.clients = []
        self.service = None
        if self.start_service:
            self.start()

    def seed(self):
        jsonl(self.records / "units.jsonl", UNITS)
        (self.records / "state.json").write_text(json.dumps(STATE))
        jsonl(self.records / "events.jsonl", [
            {"seq": i, "ts": 1_000_000 + i, "kind": "unit.updated", "unit_id": "U-0001", "data": {"n": i}, "v": 1}
            for i in range(1, 6)])
        for name in ("inbox.jsonl", "commands.jsonl"):
            (self.records / name).write_text("")

    def start(self, **options):
        merged = {"clock": self.clock, "expected_uid": os.getuid(), **self.service_options, **options}
        self.service = self.mod.CoreService(str(self.records), self.socket, **merged)
        self.service.start()
        self.addCleanup(self.service.stop)
        return self.service

    def client(self, scope="read", token=None, handshake=True):
        token = token or (self.mod._mint_token(str(self.records), scope) if scope else None)
        client = self.mod.CoreClient(self.socket, token=token)
        self.clients.append(client)
        self.addCleanup(client.close)
        if handshake:
            client.call("initialize", {"protocol": self.mod.PROTOCOL_VERSION})
        return client

    def error_code(self, client, method, params=None):
        try:
            client.call(method, params or {})
        except self.mod.ApiError as error:
            return error.code
        self.fail("%s was allowed" % method)

    def audit(self):
        return read_jsonl(self.records / "commands.jsonl")

    def inbox(self):
        return read_jsonl(self.records / "inbox.jsonl")
