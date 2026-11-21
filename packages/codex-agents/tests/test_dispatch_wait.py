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


def slow():
    """A status check the service did not answer in time (deadline_exceeded)."""
    error = DispatchError('dispatch_unavailable')
    error.timed_out = True
    return error


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
        test, self.read_count, self.status_count, self.clock = self, 0, 0, FakeClock()
        client = object.__new__(DispatchClient)
        client._endpoint = client._result_endpoint = '/nonexistent'
        client._scope, client._timeout, client.last_binding = None, 2, None
        replies = iter(replies)

        def call(operation, *args, **kwargs):
            self.status_count += operation == 'dispatch.status'
            self.clock.now += 0.01  # each call takes a little time
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
                test.read_count += 1
                value = next(reads)
                if isinstance(value, Exception):
                    raise value
                return value
        self.enterContext(mock.patch.object(dispatch_api, 'ResultReadClient', Reader))
        self.enterContext(mock.patch.object(dispatch_api, 'AttemptBinding', lambda **fields: fields))
        self.enterContext(mock.patch.object(dispatch_api, 'time', self.clock))  # unit tests never sleep
        return client

    def test_DR_1_a_failed_status_check_is_retried(self):
        client = self.client([RUNNING, slow(), slow(), PUBLISHED], [json.dumps({'summary': 'done'}).encode()])
        reply = client.dispatch('r', 1, 'x' * 64, wait_seconds=30)
        self.assertEqual({'summary': 'done'}, reply['result'])

    def test_DR_1_the_service_saying_unknown_still_ends_the_wait(self):
        client = self.client([RUNNING, {'state': 'unknown', 'binding': BINDING}], [])
        with self.assertRaises(DispatchError):
            client.dispatch('r', 1, 'x' * 64, wait_seconds=30)

    def test_DR_1_a_status_check_that_keeps_failing_ends_at_the_deadline(self):
        client = self.client([RUNNING] + [slow() for _ in range(10_000)], [])
        with self.assertRaises(DispatchError) as ended:
            client.dispatch('r', 1, 'x' * 64, wait_seconds=0.5)
        self.assertEqual('dispatch_unavailable', ended.exception.reason_code)

    def test_DR_1_a_service_that_is_gone_ends_the_wait_at_once(self):
        client = self.client([RUNNING, DispatchError('dispatch_unavailable'), PUBLISHED], [b'{}'])
        with self.assertRaises(DispatchError) as ended:
            client.dispatch('r', 1, 'x' * 64, wait_seconds=30)
        self.assertEqual('dispatch_unavailable', ended.exception.reason_code)

    def test_DR_1_a_timed_out_transport_is_marked_slow(self):
        from codex_agents.service_transport import ServiceTransportError
        client = object.__new__(DispatchClient)
        client._endpoint, client._scope, client._timeout = '/nonexistent', None, 2
        for reason, slow_answer in (('deadline_exceeded', True), ('transport_unavailable', False)):
            with self.subTest(reason), mock.patch.object(dispatch_api, 'dispatch_request', return_value=None), \
                    mock.patch.object(dispatch_api, 'encode_dispatch_request', return_value=b'{}'), \
                    mock.patch.object(dispatch_api, 'exchange_frame', side_effect=ServiceTransportError(reason)):
                with self.assertRaises(DispatchError) as ended:
                    client.call('dispatch.status', 'r', 1, 'x' * 64)
                self.assertEqual(slow_answer, getattr(ended.exception, 'timed_out', False))

    def test_DR_1_another_refusal_is_not_retried(self):
        client = self.client([RUNNING, DispatchError('dispatch_not_found'), PUBLISHED], [b'{}'])
        with self.assertRaises(DispatchError) as ended:
            client.dispatch('r', 1, 'x' * 64, wait_seconds=30)
        self.assertEqual('dispatch_not_found', ended.exception.reason_code)

    def test_DR_2_reading_the_published_result_is_retried_while_it_is_still_published(self):
        client = self.client([RUNNING, PUBLISHED, PUBLISHED], [OSError('slow'), json.dumps({'summary': 'done'}).encode()])
        self.assertEqual({'summary': 'done'}, client.dispatch('r', 1, 'x' * 64, wait_seconds=30)['result'])

    def test_DR_2_a_timed_out_recheck_is_never_taken_as_permission_to_read(self):
        reads = [OSError('slow'), json.dumps({'summary': 'done'}).encode()]
        client = self.client([RUNNING, PUBLISHED, slow(), slow(), PUBLISHED], reads)
        self.assertEqual({'summary': 'done'}, client.dispatch('r', 1, 'x' * 64, wait_seconds=30)['result'])
        self.assertEqual(2, self.read_count)  # one read before the timeouts, one after a fresh published
        self.assertEqual(4, self.status_count)  # the second read waited for the fresh published answer

    def test_DR_2_a_retired_answer_ends_a_reread(self):
        client = self.client([RUNNING, PUBLISHED, {'state': 'retired', 'binding': BINDING}], [OSError('slow')])
        with self.assertRaises(DispatchError):
            client.dispatch('r', 1, 'x' * 64, wait_seconds=30)
        self.assertEqual(1, self.read_count)

    def test_DR_1_a_service_that_stays_slow_is_retried_until_the_deadline_and_no_longer(self):
        client = self.client([RUNNING] + [slow() for _ in range(10_000)], [])
        with self.assertRaises(DispatchError):
            client.dispatch('r', 1, 'x' * 64, wait_seconds=0.5)
        self.assertGreater(self.status_count, 3)  # it did retry
        self.assertLessEqual(self.clock.now - 1000.0, 0.5 + 0.06)  # and stopped at the deadline

    def test_DR_2_a_failed_read_from_a_service_that_is_gone_ends_the_wait(self):
        client = self.client([RUNNING, PUBLISHED, DispatchError('dispatch_unavailable')], [OSError('gone')])
        with self.assertRaises(DispatchError):
            client.dispatch('r', 1, 'x' * 64, wait_seconds=30)

    def test_DR_3_a_dispatch_whose_reply_was_lost_is_answered_with_the_result_by_a_later_dispatch(self):
        """The first dispatch lost its reply (the submit never answered); a later dispatch for the same run
        and inputs finds the published result and reads it."""
        client = self.client([slow()], [])
        with self.assertRaises(DispatchError):
            client.dispatch('r', 1, 'x' * 64, wait_seconds=30)
        later = self.client([PUBLISHED], [json.dumps({'summary': 'done'}).encode()])
        self.assertEqual({'summary': 'done'}, later.dispatch('r', 1, 'x' * 64, wait_seconds=30)['result'])
        self.assertEqual(1, self.read_count)


if __name__ == '__main__':
    unittest.main()
