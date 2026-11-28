"""SDK contract checks at the HTTP boundary; no robot connections."""
import io
from http.client import BadStatusLine
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from src.dev_client import DevClient, DevClientError


class Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())


class Endpoint:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.response = None

    def open(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.calls.append((request, body, timeout))
        if self.failure:
            raise self.failure
        if self.response is not None:
            return Response(self.response)
        if request.full_url.endswith('/acquire'):
            return Response({'lease': 'opaque-lease', 'lease_active': True})
        return Response({'mode': 'hardware', 'environment': 'dev', 'lease_active': False})


class DevClientTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = Endpoint()
        patcher = patch(DevClient.__module__ + '.build_opener', return_value=self.endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = DevClient('http://127.0.0.1:8080/', timeout=0.75)

    def test_status_and_acquire_use_trusted_lan_api(self):
        self.client.status()
        request, body, timeout = self.endpoint.calls[-1]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8080/api/status')
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(body)
        self.assertEqual(timeout, 0.75)
        self.client.acquire('offline script')
        request, body, _ = self.endpoint.calls[-1]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(body, {'owner': 'offline script'})
        self.assertEqual(self.client.lease, 'opaque-lease')

    def test_motion_requires_explicit_acquire(self):
        for method in (self.client.stand, self.client.heartbeat,
                       lambda: self.client.velocity(vx=0.1),
                       lambda: self.client.arm_jog('b', 1)):
            with self.assertRaises(DevClientError):
                method()
        self.assertEqual(self.endpoint.calls, [])

    def test_arm_operation_lookup_does_not_replay_and_arm_stop_forgets_lease(self):
        self.client.acquire()
        self.client.arm_operation('operation-1')
        request, body, _ = self.endpoint.calls[-1]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8080/api/arm/operation')
        self.assertEqual(body, {'id': 'operation-1'})
        self.assertEqual(self.client.lease, 'opaque-lease')
        self.client.arm_stop()
        request, body, _ = self.endpoint.calls[-1]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8080/api/arm/stop')
        self.assertEqual(body, {'lease': 'opaque-lease'})
        self.assertIsNone(self.client.lease)
        self.client.arm_stop()
        self.assertEqual(self.endpoint.calls[-1][1], {})

    def test_commands_use_one_increasing_sequence_and_expected_units(self):
        self.client.acquire()
        self.client.stand()
        self.client.velocity(vx=0.1, vy=-0.05, yaw=0.2, ttl=0.3)
        self.client.arm_jog('b', -1, speed=2)
        bodies = [call[1] for call in self.endpoint.calls[1:]]
        self.assertEqual(bodies, [
            {'lease': 'opaque-lease', 'seq': 1},
            {'lease': 'opaque-lease', 'seq': 2, 'vx': 0.1, 'vy': -0.05, 'yaw': 0.2, 'ttl': 0.3},
            {'lease': 'opaque-lease', 'seq': 3, 'joint': 'b', 'delta': -1, 'speed': 2},
        ])

    def test_concurrent_motion_keeps_wire_sequence_in_order(self):
        self.client.acquire()
        original = self.endpoint.open
        def slow_first(request, timeout):
            if b'"seq": 1' in request.data:
                time.sleep(0.02)
            return original(request, timeout)
        self.endpoint.open = slow_first
        threads = [threading.Thread(target=self.client.velocity, kwargs={'vx': 0.1}) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertEqual([row[1]['seq'] for row in self.endpoint.calls[1:]], list(range(1, 11)))

    def start_call(self, operation):
        done = threading.Event()
        result = {'values': [], 'errors': []}
        def run():
            try:
                result['values'].append(operation())
            except Exception as error:
                result['errors'].append(error)
            finally:
                done.set()
        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(thread.join, 2)
        return done, result

    def delay_response(self, action, failure=None):
        entered, proceed = threading.Event(), threading.Event()
        original = self.endpoint.open
        def delayed(request, timeout):
            response = original(request, timeout)
            if request.full_url.endswith('/' + action):
                entered.set()
                if not proceed.wait(3):
                    raise AssertionError('Test did not release its delayed response')
                if failure:
                    raise failure
            return response
        self.endpoint.open = delayed
        self.addCleanup(proceed.set)
        return entered, proceed

    def test_slow_unowned_reads_do_not_block_heartbeat_or_stops(self):
        for read_name in ('arm_state', 'arm_reconnect', 'status'):
            for urgent_name in ('heartbeat', 'stop', 'estop'):
                with self.subTest(read=read_name, urgent=urgent_name):
                    self.client = DevClient('http://127.0.0.1:8080')
                    self.endpoint = Endpoint()
                    self.client._opener = self.endpoint
                    self.client.acquire()
                    action = {'arm_state': 'arm/state', 'arm_reconnect': 'arm/reconnect',
                              'status': 'status'}[read_name]
                    entered, proceed = self.delay_response(action)
                    read_done, read_result = self.start_call(getattr(self.client, read_name))
                    self.assertTrue(entered.wait(1))
                    urgent_done, urgent_result = self.start_call(getattr(self.client, urgent_name))
                    try:
                        self.assertTrue(urgent_done.wait(0.5), 'Read blocked ' + urgent_name)
                        self.assertFalse(urgent_result['errors'])
                    finally:
                        proceed.set()
                        self.assertTrue(read_done.wait(1))
                        self.assertTrue(urgent_done.wait(1))
                    self.assertFalse(read_result['errors'])

    def test_delayed_unowned_failure_cannot_forget_a_reacquired_lease(self):
        self.client.acquire()
        entered, proceed = self.delay_response('arm/state', URLError('delayed failure'))
        read_done, read_result = self.start_call(self.client.arm_state)
        self.assertTrue(entered.wait(1))
        def replace_lease():
            self.client.stop()
            self.client.acquire('new-session')
        replaced, replacement = self.start_call(replace_lease)
        try:
            self.assertTrue(replaced.wait(0.5), 'Old read blocked stop and reacquire')
            self.assertFalse(replacement['errors'])
            new_lease = self.client.lease
        finally:
            proceed.set()
            self.assertTrue(read_done.wait(1))
            self.assertTrue(replaced.wait(1))
        self.assertIsInstance(read_result['errors'][0], DevClientError)
        self.assertEqual(self.client.lease, new_lease)
        self.client.heartbeat()

    def test_stop_supersedes_a_delayed_motion_response(self):
        self.client.acquire()
        entered, proceed = self.delay_response('dog/velocity')
        motion_done, motion_result = self.start_call(self.client.velocity)
        self.assertTrue(entered.wait(1))
        stopped, stop_result = self.start_call(self.client.stop)
        try:
            self.assertTrue(stopped.wait(0.5), 'Motion response blocked stop')
            self.assertFalse(stop_result['errors'])
            self.assertIsNone(self.client.lease)
        finally:
            proceed.set()
            self.assertTrue(motion_done.wait(1))
            self.assertTrue(stopped.wait(1))
        self.assertEqual(motion_result['values'], [])
        self.assertIsInstance(motion_result['errors'][0], DevClientError)
        self.assertIsNone(self.client.lease)

    def test_stop_supersedes_a_delayed_acquire_response(self):
        entered, proceed = self.delay_response('control/acquire')
        acquired, acquire_result = self.start_call(self.client.acquire)
        self.assertTrue(entered.wait(1))
        stopped, stop_result = self.start_call(self.client.stop)
        try:
            self.assertTrue(stopped.wait(0.5), 'Acquire response blocked stop')
            self.assertFalse(stop_result['errors'])
        finally:
            proceed.set()
            self.assertTrue(acquired.wait(1))
            self.assertTrue(stopped.wait(1))
        self.assertEqual(acquire_result['values'], [])
        self.assertIsInstance(acquire_result['errors'][0], DevClientError)
        self.assertIsNone(self.client.lease)

    def test_stop_discards_lease_and_observer_stop_needs_no_acquire(self):
        self.client.acquire()
        self.client.stop()
        self.assertIsNone(self.client.lease)
        with self.assertRaises(DevClientError):
            self.client.velocity(vx=0.1)
        self.client.stop()
        self.assertEqual(self.endpoint.calls[-1][1], {})

    def test_error_clears_lease_and_never_retries(self):
        self.client.acquire()
        self.endpoint.failure = HTTPError('http://127.0.0.1/api/dog/velocity', 409, 'conflict', {},
                                          io.BytesIO(b'{"error":"lease expired"}'))
        with self.assertRaisesRegex(DevClientError, 'lease expired') as error:
            self.client.velocity(vx=0.1)
        self.assertEqual(error.exception.status, 409)
        self.assertIsNone(self.client.lease)
        self.assertEqual(len(self.endpoint.calls), 2)
        with self.assertRaises(DevClientError):
            self.client.stand()
        self.assertEqual(len(self.endpoint.calls), 2)

    def test_network_error_clears_lease_and_is_not_retried(self):
        self.client.acquire()
        self.endpoint.failure = URLError('timed out')
        with self.assertRaises(DevClientError):
            self.client.heartbeat()
        self.assertIsNone(self.client.lease)
        self.assertEqual(len(self.endpoint.calls), 2)

    def test_broken_http_response_clears_lease(self):
        self.client.acquire()
        self.endpoint.failure = BadStatusLine('invalid HTTP response')
        with self.assertRaises(DevClientError):
            self.client.heartbeat()
        self.assertIsNone(self.client.lease)
        self.assertEqual(len(self.endpoint.calls), 2)

    def test_context_releases_only_explicitly_acquired_lease(self):
        with self.client as client:
            self.assertEqual(self.endpoint.calls, [])
            client.acquire('test')
        self.assertEqual(self.endpoint.calls[-1][0].full_url, 'http://127.0.0.1:8080/api/control/release')
        self.assertEqual(self.endpoint.calls[-1][1], {'lease': 'opaque-lease'})
        self.assertIsNone(self.client.lease)

    def test_cleanup_preserves_original_script_exception(self):
        with self.assertRaisesRegex(ValueError, 'script failed'):
            with self.client:
                self.client.acquire()
                self.endpoint.failure = URLError('offline')
                raise ValueError('script failed')
        self.assertIsNone(self.client.lease)

    def test_release_estop_and_reset_forget_ownership(self):
        for action in (self.client.release, self.client.estop,
                       lambda: self.client.reset('observed-safety-revision')):
            self.client.acquire()
            action()
            self.assertIsNone(self.client.lease)

    def test_reset_uses_the_explicitly_observed_revision_without_fetching(self):
        with self.assertRaises(TypeError):
            self.client.reset()
        self.assertEqual(self.endpoint.calls, [])
        self.client.reset('observed-safety-revision')
        self.assertEqual(len(self.endpoint.calls), 1)
        request, body, _ = self.endpoint.calls[0]
        self.assertTrue(request.full_url.endswith('/control/reset'))
        self.assertEqual(body, {'revision': 'observed-safety-revision'})

    def test_read_and_reconnect_arm_are_explicit_posts(self):
        self.client.arm_state()
        self.client.arm_reconnect()
        self.assertEqual([row[0].full_url.rsplit('/', 1)[-1] for row in self.endpoint.calls], ['state', 'reconnect'])
        self.assertTrue(all(row[0].get_method() == 'POST' for row in self.endpoint.calls))
        self.assertTrue(all(row[1] == {} for row in self.endpoint.calls))

    def test_malformed_or_oversize_reply_never_keeps_ownership(self):
        for reply in (b'not json', b'[]', b'{}' + b' ' * 65536, b'{"lease":""}'):
            self.endpoint.response = reply
            with self.assertRaises(DevClientError):
                self.client.acquire()
            self.assertIsNone(self.client.lease)

    def test_invalid_json_numbers_are_not_sent(self):
        self.client.acquire()
        with self.assertRaises((ValueError, DevClientError)):
            self.client.velocity(vx=float('nan'))
        self.assertEqual(len(self.endpoint.calls), 1)

    def test_invalid_origins_and_unbounded_timeouts_are_rejected(self):
        for url in ('file:///tmp/server', 'http://user:password@host', 'http://host/?mode=x',
                    'http://host/#control', 'http://host/control'):
            with self.assertRaises(ValueError):
                DevClient(url)
        for timeout in (0, -1, float('inf'), float('nan'), 61, True):
            with self.assertRaises(ValueError):
                DevClient('http://localhost', timeout=timeout)


if __name__ == '__main__':
    unittest.main()
