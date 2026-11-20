"""O10 DR: one failed or slow status check never ends a dispatch's wait (PLAN DR-1, DR-2).

Unit stage: the dispatch client's wait with its service calls replaced, so a lost reply can be placed
exactly.
"""
import json
import unittest
from unittest import mock

import tests.support  # noqa: F401
from codex_agents import dispatch_api
from codex_agents.dispatch_api import DispatchClient, DispatchError

BINDING = {'orchestration_handle': 'o', 'run_handle': 'r', 'attempt': 1}
RUNNING = {'state': 'running', 'binding': BINDING}
PUBLISHED = {'state': 'published', 'binding': BINDING, 'delivery': {'result_digest': 'd' * 64},
             'report': {'outcome': 'success'}}


class FakeClock:
    """time for the dispatch wait: sleeping moves the clock on."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.001)


class DispatchWaitTests(unittest.TestCase):
    def client(self, replies, reads):
        client = object.__new__(DispatchClient)
        client._endpoint = client._result_endpoint = '/nonexistent'
        client._scope, client._timeout, client.last_binding = None, 2, None
        replies = iter(replies)

        def call(operation, *args, **kwargs):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply
        client.call = call
        reads = iter(reads)

        class Reader:
            def __init__(self, *args, **kwargs):
                pass

            def read_result(self, binding, digest):
                value = next(reads)
                if isinstance(value, Exception):
                    raise value
                return value
        self.enterContext(mock.patch.object(dispatch_api, 'ResultReadClient', Reader))
        self.enterContext(mock.patch.object(dispatch_api, 'AttemptBinding', lambda **fields: fields))
        self.enterContext(mock.patch.object(dispatch_api, 'time', FakeClock()))  # unit tests never sleep
        return client

    def test_DR_1_a_failed_status_check_is_retried(self):
        client = self.client([RUNNING, DispatchError('dispatch_unavailable'), DispatchError('dispatch_unavailable'),
                              PUBLISHED], [json.dumps({'summary': 'done'}).encode()])
        reply = client.dispatch('r', 1, 'x' * 64, wait_seconds=30)
        self.assertEqual({'summary': 'done'}, reply['result'])

    def test_DR_1_the_service_saying_unknown_still_ends_the_wait(self):
        client = self.client([RUNNING, {'state': 'unknown', 'binding': BINDING}], [])
        with self.assertRaises(DispatchError):
            client.dispatch('r', 1, 'x' * 64, wait_seconds=30)

    def test_DR_1_a_status_check_that_keeps_failing_ends_at_the_deadline(self):
        client = self.client([RUNNING] + [DispatchError('dispatch_unavailable')] * 10_000, [])
        with self.assertRaises(DispatchError) as ended:
            client.dispatch('r', 1, 'x' * 64, wait_seconds=0.5)
        self.assertEqual('dispatch_unavailable', ended.exception.reason_code)

    def test_DR_1_another_refusal_is_not_retried(self):
        client = self.client([RUNNING, DispatchError('dispatch_not_found'), PUBLISHED], [b'{}'])
        with self.assertRaises(DispatchError) as ended:
            client.dispatch('r', 1, 'x' * 64, wait_seconds=30)
        self.assertEqual('dispatch_not_found', ended.exception.reason_code)

    def test_DR_2_reading_the_published_result_is_retried(self):
        client = self.client([RUNNING, PUBLISHED], [OSError('slow'), json.dumps({'summary': 'done'}).encode()])
        self.assertEqual({'summary': 'done'}, client.dispatch('r', 1, 'x' * 64, wait_seconds=30)['result'])


if __name__ == '__main__':
    unittest.main()
