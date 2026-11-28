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


def action_status(phase='running', lease_active=True):
    return {
        'mode': 'hardware', 'environment': 'dev', 'estop': False,
        'safety_revision': 'revision-1', 'lease_active': lease_active,
        'dog': {
            'phase': 'moving' if phase == 'running' else 'disarmed',
            'velocity': {'vx': 0.1 if phase == 'running' else 0.0, 'vy': 0.0, 'yaw': 0.0},
            'limits': {'vx': 0.2, 'vy': 0.1, 'yaw': 0.3, 'ttl': 0.5},
            'error': None, 'stop_verified': False, 'simulated': True,
            'motion_enabled': True, 'actions_enabled': False,
            'feedback': {
                'observed': False, 'simulated': True, 'fresh': True, 'age_s': 0.0,
                'basic_state': 6, 'gait_state': 0, 'motion_state': 0,
                'standing': True, 'motion_ready': True, 'reason': None,
            },
            'action': {
                'id': 'action-1', 'name': 'forward', 'speed': 0.1, 'duration': 1.0,
                'phase': phase, 'reason': None, 'remaining_s': 1.0 if phase == 'running' else 0.0,
            },
        },
        'arm': {'motion_enabled': True, 'reason': None, 'position_deg': None, 'simulated': True},
    }


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

    def test_dog_actions_send_one_request_with_shared_sequence_and_positive_speed(self):
        self.assertTrue(callable(getattr(self.client, 'dog_action', None)))
        self.client.acquire()
        self.client.stand()
        self.endpoint.response = action_status()
        result = self.client.dog_action('backward', speed=0.15, duration=2.5)
        self.assertEqual(result['dog']['action']['id'], 'action-1')
        self.client.forward()
        self.client.backward(speed=0.12, duration=0.8)
        self.client.turn_left()
        self.client.turn_right(speed=0.25, duration=2)
        self.client.velocity(vx=0.1)
        actions = self.endpoint.calls[2:-1]
        self.assertEqual([row[0].full_url for row in actions],
                         ['http://127.0.0.1:8080/api/dog/action'] * 5)
        self.assertTrue(all(row[0].get_method() == 'POST' for row in actions))
        self.assertEqual([row[1] for row in actions], [
            {'lease': 'opaque-lease', 'seq': 2, 'name': 'backward', 'speed': 0.15, 'duration': 2.5},
            {'lease': 'opaque-lease', 'seq': 3, 'name': 'forward', 'speed': 0.1, 'duration': 1.0},
            {'lease': 'opaque-lease', 'seq': 4, 'name': 'backward', 'speed': 0.12, 'duration': 0.8},
            {'lease': 'opaque-lease', 'seq': 5, 'name': 'turn_left', 'speed': 0.2, 'duration': 1.0},
            {'lease': 'opaque-lease', 'seq': 6, 'name': 'turn_right', 'speed': 0.25, 'duration': 2},
        ])
        self.assertEqual(self.endpoint.calls[-1][1]['seq'], 7)
        self.assertEqual(self.client.lease, 'opaque-lease')

    def test_dog_actions_require_explicit_ownership(self):
        for name in ('dog_action', 'forward', 'backward', 'turn_left', 'turn_right'):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(self.client, name, None)))
                args = ('forward', 0.1, 1.0) if name == 'dog_action' else ()
                with self.assertRaises(DevClientError):
                    getattr(self.client, name)(*args)
        self.assertEqual(self.endpoint.calls, [])

    def test_dog_action_error_forgets_ownership_without_retry_or_implicit_preparation(self):
        self.assertTrue(callable(getattr(self.client, 'forward', None)))
        self.client.acquire()
        self.endpoint.failure = HTTPError('http://127.0.0.1/api/dog/action', 423, 'blocked', {},
                                          io.BytesIO(b'{"error":"fresh standing feedback required"}'))
        with self.assertRaisesRegex(DevClientError, 'fresh standing feedback required') as error:
            self.client.forward()
        self.assertEqual(error.exception.status, 423)
        self.assertIsNone(self.client.lease)
        self.assertEqual(len(self.endpoint.calls), 2)
        self.assertTrue(self.endpoint.calls[-1][0].full_url.endswith('/dog/action'))

    def test_terminal_action_status_forgets_lease_and_context_cleanup_sends_no_release(self):
        for phase in ('completed', 'cancelled', 'fault'):
            for method in ('status', 'heartbeat'):
                with self.subTest(phase=phase, method=method):
                    self.endpoint.response = None
                    with self.client as client:
                        client.acquire()
                        self.endpoint.response = action_status(phase, lease_active=False)
                        state = getattr(client, method)()
                        self.assertEqual(state['dog']['action']['phase'], phase)
                        self.assertIsNone(client.lease)
                        calls_before_exit = len(self.endpoint.calls)
                    self.assertEqual(len(self.endpoint.calls), calls_before_exit)

    def test_running_or_old_terminal_action_with_active_lease_preserves_ownership(self):
        self.client.acquire()
        for phase, active in (('running', True), ('completed', True), ('running', False)):
            with self.subTest(phase=phase, lease_active=active):
                self.endpoint.response = action_status(phase, lease_active=active)
                self.client.status()
                self.assertEqual(self.client.lease, 'opaque-lease')

    def test_own_terminal_action_with_successor_lease_clears_local_ownership(self):
        for phase in ('completed', 'cancelled', 'fault'):
            with self.subTest(phase=phase):
                self.client = DevClient('http://127.0.0.1:8080')
                self.endpoint.response = None
                self.client.acquire()
                self.endpoint.response = action_status()
                self.client.forward()
                self.endpoint.response = action_status(phase, lease_active=True)
                before = len(self.endpoint.calls)
                self.client.status()
                self.assertIsNone(self.client.lease)
                self.assertEqual(len(self.endpoint.calls), before + 1)
                self.assertTrue(self.endpoint.calls[-1][0].full_url.endswith('/status'))
                self.client.close()
                self.assertEqual(len(self.endpoint.calls), before + 1)
                self.endpoint.response = None
                self.client.acquire('later-session')
                self.assertEqual(self.endpoint.calls[-1][1], {'owner': 'later-session'})
                self.client.release()

    def test_another_terminal_action_with_active_lease_preserves_local_ownership(self):
        self.client.acquire()
        self.endpoint.response = action_status()
        self.client.forward()
        self.endpoint.response = action_status('completed', lease_active=True)
        self.endpoint.response['dog']['action']['id'] = 'another-action'
        self.client.status()
        self.assertEqual(self.client.lease, 'opaque-lease')

    def test_delayed_terminal_status_cannot_forget_reacquired_lease(self):
        self.client.acquire()
        self.endpoint.response = action_status('completed', lease_active=False)
        entered, proceed = self.delay_response('status')
        done, result = self.start_call(self.client.status)
        self.assertTrue(entered.wait(1))
        try:
            self.endpoint.response = None
            self.client.stop()
            self.client.acquire('new-session')
            new_lease = self.client.lease
        finally:
            proceed.set()
            self.assertTrue(done.wait(1))
        self.assertFalse(result['errors'])
        self.assertEqual(result['values'][0]['dog']['action']['phase'], 'completed')
        self.assertEqual(self.client.lease, new_lease)
        self.client.heartbeat()

    def test_status_started_during_acquire_cannot_clear_the_new_lease(self):
        acquire_entered, finish_acquire = self.delay_response('control/acquire')
        acquired, acquisition = self.start_call(self.client.acquire)
        self.assertTrue(acquire_entered.wait(1))
        self.endpoint.response = action_status('completed', lease_active=False)
        status_entered, finish_status = self.delay_response('status')
        read, result = self.start_call(self.client.status)
        self.assertTrue(status_entered.wait(1))
        try:
            finish_acquire.set()
            self.assertTrue(acquired.wait(1))
            self.assertFalse(acquisition['errors'])
            self.assertEqual(self.client.lease, 'opaque-lease')
        finally:
            finish_status.set()
            self.assertTrue(read.wait(1))
        self.assertFalse(result['errors'])
        self.assertEqual(result['values'][0]['dog']['action']['phase'], 'completed')
        self.assertEqual(self.client.lease, 'opaque-lease')

    def completed_action_release(self, status, failure=None):
        self.client.acquire()
        self.endpoint.response = action_status()
        self.client.forward()
        original = self.endpoint.open
        def revoked(request, timeout):
            response = original(request, timeout)
            if request.full_url.endswith('/control/release'):
                raise HTTPError(request.full_url, 409, 'conflict', {},
                                io.BytesIO(b'{"error":"Invalid or expired control lease"}'))
            if request.full_url.endswith('/status'):
                if failure is not None:
                    raise failure
                return Response(status)
            return response
        self.endpoint.open = revoked

    def test_context_cleanup_accepts_completed_action_without_explicit_poll(self):
        self.completed_action_release(action_status('completed', lease_active=False))
        with self.client:
            pass
        self.assertIsNone(self.client.lease)
        self.assertEqual([row[0].full_url.rsplit('/api/', 1)[1] for row in self.endpoint.calls],
                         ['control/acquire', 'dog/action', 'control/release', 'status'])
        self.assertEqual(self.endpoint.calls[-2][1], {'lease': 'opaque-lease'})

    def test_cleanup_of_completed_action_preserves_another_controllers_lease(self):
        self.completed_action_release(action_status('completed', lease_active=True))
        self.client.close()
        self.assertIsNone(self.client.lease)
        self.assertEqual([row[0].full_url.rsplit('/api/', 1)[1] for row in self.endpoint.calls],
                         ['control/acquire', 'dog/action', 'control/release', 'status'])
        self.assertEqual(self.endpoint.calls[-2][1], {'lease': 'opaque-lease'})

    def test_cleanup_keeps_release_errors_for_unconfirmed_completion(self):
        for phase, action_id in (('running', 'action-1'), ('cancelled', 'action-1'),
                                 ('fault', 'action-1'), ('completed', 'another-action')):
            with self.subTest(phase=phase, action_id=action_id):
                self.endpoint = Endpoint()
                self.client._opener = self.endpoint
                status = action_status(phase, lease_active=False)
                status['dog']['action']['id'] = action_id
                self.completed_action_release(status)
                with self.assertRaises(DevClientError) as error:
                    self.client.close()
                self.assertEqual(error.exception.status, 409)
                self.assertIsNone(self.client.lease)

    def test_cleanup_preserves_status_transport_failure_after_release_conflict(self):
        self.completed_action_release(action_status('completed', lease_active=False),
                                      failure=URLError('confirmation unavailable'))
        with self.assertRaisesRegex(DevClientError, 'confirmation unavailable'):
            self.client.close()

    def test_cleanup_keeps_transport_failure_without_querying_status(self):
        self.client.acquire()
        self.endpoint.response = action_status()
        self.client.forward()
        self.endpoint.failure = URLError('release unavailable')
        with self.assertRaisesRegex(DevClientError, 'release unavailable'):
            self.client.close()
        self.assertEqual(len(self.endpoint.calls), 3)

    def test_explicit_release_stays_strict_after_action_completion(self):
        self.completed_action_release(action_status('completed', lease_active=False))
        with self.assertRaises(DevClientError) as error:
            self.client.release()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(len(self.endpoint.calls), 3)

    def test_cleanup_does_not_associate_an_old_action_with_a_new_lease(self):
        self.completed_action_release(action_status('completed', lease_active=False))
        self.client.stop()
        self.endpoint.response = None
        self.client.acquire('new-session')
        with self.assertRaises(DevClientError) as error:
            self.client.close()
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(any(row[0].full_url.endswith('/status') for row in self.endpoint.calls))

    def test_stop_supersedes_delayed_dog_action_response(self):
        self.assertTrue(callable(getattr(self.client, 'forward', None)))
        self.client.acquire()
        entered, proceed = self.delay_response('dog/action')
        done, result = self.start_call(self.client.forward)
        self.assertTrue(entered.wait(1))
        try:
            self.client.stop()
        finally:
            proceed.set()
            self.assertTrue(done.wait(1))
        self.assertEqual(result['values'], [])
        self.assertIsInstance(result['errors'][0], DevClientError)
        self.assertIsNone(self.client.lease)

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
