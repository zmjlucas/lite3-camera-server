"""SDK contract checks at the HTTP boundary; no robot connections."""
import io
from http.client import BadStatusLine, HTTPMessage
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from src.lite3_dev_server import DevClient, DevClientError


DEEP_JSON = b'{"extra":' + b'[' * 20000 + b'0' + b']' * 20000 + b'}'


def protocol_headers(versions=('1',)):
    headers = HTTPMessage()
    for version in versions:
        headers.add_header('X-Lite3-Dev-Protocol', version)
    return headers


class Response(io.BytesIO):
    def __init__(self, body, versions=('1',)):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = protocol_headers(versions)


def control_status(mode='simulation', lease_active=False):
    simulated = mode == 'simulation'
    return {
        'mode': mode,
        'estop': False,
        'safety_revision': 'safety-revision',
        'lease_active': lease_active,
        'dog': {
            'phase': 'disarmed', 'velocity': {'vx': 0, 'vy': 0, 'yaw': 0},
            'limits': {'vx': 0.2, 'vy': 0.1, 'yaw': 0.3, 'ttl': 0.5},
            'error': None, 'stop_verified': False, 'simulated': simulated,
            'motion_enabled': simulated,
        },
        'arm': {
            'motion_enabled': simulated, 'reason': 'Simulation' if simulated else None,
            'position_deg': dict(b=0, s=0, e=0, w=0, h=0) if simulated else None,
            'simulated': simulated,
        },
    }


def arm_observation():
    return {
        'position_deg': dict(b=1, s=2, e=3, w=4), 'gripper_position_deg': None,
        'observed': True, 'motion_enabled': False, 'reason': None,
    }


def arm_operation():
    sample = {'position_deg': dict(b=1, s=2, e=3, w=4), 'sample_monotonic': 123.4}
    return {
        'operation_id': 'operation-1', 'phase': 'moving', 'reason': None,
        'joint': 'b', 'delta_deg': 0.5, 'speed_deg_s': 1,
        'observed_start': sample, 'observed_end': None,
        'targets_written': [dict(b=1.5, s=2, e=3, w=4, h=0)],
        'hold_status': 'not_needed', 'observed_stationary': None,
        'stop_verified': False, 'gripper_position_deg': None,
    }


def arm_capabilities():
    return {
        'joints': ['b', 'e', 'h'], 'max_delta_deg': {'b': 0.5, 'e': 2, 'h': 0.5},
        'max_speed_deg_s': 1, 'max_travel_deg': 3, 'asynchronous': True,
        'stop_verified': False, 'gripper_feedback': False,
    }


class Endpoint:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.response = None
        self.versions = ('1',)
        self.lease = 'opaque-lease'

    def open(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.calls.append((request, body, timeout))
        if self.failure:
            raise self.failure
        if self.response is not None:
            return Response(self.response, self.versions)
        action = request.full_url.split('/api/', 1)[1]
        if action == 'control/acquire':
            value = {'lease': self.lease, 'status': control_status(lease_active=True)}
        elif action == 'arm/operation':
            value = arm_operation()
        elif action == 'arm/state':
            value = arm_observation()
        elif action == 'arm/reconnect':
            value = {'connected': False, 'reconnect_required': False,
                     'motion_enabled': False, 'reason': 'Reconnect scheduled'}
        else:
            value = control_status(lease_active=action in (
                'control/heartbeat', 'dog/stand', 'dog/velocity', 'arm/jog',
            ))
        return Response(value, self.versions)


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

    def delay_response(self, action, failure=None, reply=None, versions=('1',)):
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
                if reply is not None:
                    response.close()
                    return Response(reply, versions)
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

    def test_delayed_invalid_replies_cannot_forget_a_reacquired_lease(self):
        for kind, reply, versions in (
            ('shape', {}, ('1',)), ('version', arm_observation(), ('1', '1')),
            ('missing_version', arm_observation(), ()), ('depth', DEEP_JSON, ('1',)),
        ):
            with self.subTest(kind=kind):
                self.endpoint.lease = 'old-lease'
                self.client.acquire()
                original_open = self.endpoint.open
                entered, proceed = self.delay_response('arm/state', reply=reply, versions=versions)
                read_done, read_result = self.start_call(self.client.arm_state)
                try:
                    self.assertTrue(entered.wait(1))
                    self.client.stop()
                    self.endpoint.lease = 'new-lease'
                    self.client.acquire('new-session')
                finally:
                    proceed.set()
                    self.assertTrue(read_done.wait(1))
                    self.endpoint.open = original_open
                try:
                    self.assertFalse(read_result['values'])
                    self.assertIsInstance(read_result['errors'][0], DevClientError)
                    self.assertEqual(self.client.lease, 'new-lease')
                    self.client.heartbeat()
                finally:
                    self.client.release()

    def test_invalid_read_started_during_acquire_cannot_forget_the_adopted_lease(self):
        acquire_entered, deliver_acquire = self.delay_response('control/acquire')
        read_entered, deliver_read = self.delay_response('arm/state', reply={})
        acquired, acquire_result = self.start_call(self.client.acquire)
        try:
            self.assertTrue(acquire_entered.wait(1))
            read_done, read_result = self.start_call(self.client.arm_state)
            self.assertTrue(read_entered.wait(1))
            deliver_acquire.set()
            self.assertTrue(acquired.wait(1))
            self.assertFalse(acquire_result['errors'])
            self.assertEqual(self.client.lease, 'opaque-lease')

            deliver_read.set()
            self.assertTrue(read_done.wait(1))
            self.assertIsInstance(read_result['errors'][0], DevClientError)
            self.assertEqual(self.client.lease, 'opaque-lease')
            self.client.heartbeat()
        finally:
            deliver_acquire.set()
            deliver_read.set()

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
        self.endpoint.failure = HTTPError('http://127.0.0.1/api/dog/velocity', 409, 'conflict', protocol_headers(),
                                          io.BytesIO(b'{"error":"lease expired","code":"lease_required"}'))
        with self.assertRaisesRegex(DevClientError, 'lease expired') as error:
            self.client.velocity(vx=0.1)
        self.assertEqual(error.exception.status, 409)
        self.assertIsNone(self.client.lease)
        self.assertEqual(len(self.endpoint.calls), 2)
        with self.assertRaises(DevClientError):
            self.client.stand()
        self.assertEqual(len(self.endpoint.calls), 2)

    def test_unreadable_error_envelope_preserves_http_status(self):
        oversized_json = b'{"error":"ignored"}' + b' ' * 65536
        for body in (DEEP_JSON, oversized_json, b'{"error":false}',
                     b'{"error":"ignored","code":false}', b'{"error":"first","error":"second"}'):
            with self.subTest(length=len(body)):
                self.client.acquire()
                self.endpoint.failure = HTTPError('http://127.0.0.1/api/control/heartbeat', 409,
                                                  'conflict', protocol_headers(), io.BytesIO(body))
                try:
                    with self.assertRaisesRegex(DevClientError, 'HTTP 409') as error:
                        self.client.heartbeat()
                    self.assertEqual(error.exception.status, 409)
                    self.assertIsNone(self.client.lease)
                finally:
                    self.endpoint.failure = None
                    self.client.release()

    def test_http_error_requires_one_v1_header_and_keeps_http_status(self):
        for versions in (None, (), ('2',), ('',), ('1, 1',), ('1', '1'), ('1', '2')):
            with self.subTest(versions=versions):
                self.client.acquire()
                headers = None if versions is None else protocol_headers(versions)
                self.endpoint.failure = HTTPError('http://127.0.0.1/api/dog/velocity', 503,
                                                  'unavailable', headers,
                                                  io.BytesIO(b'{"error":"foreign error format"}'))
                requests = len(self.endpoint.calls)
                try:
                    with self.assertRaisesRegex(DevClientError, 'protocol') as error:
                        self.client.velocity(vx=0.1)
                    self.assertEqual(error.exception.status, 503)
                    self.assertIsNone(self.client.lease)
                    self.assertEqual(len(self.endpoint.calls), requests + 1)
                finally:
                    self.endpoint.failure = None
                    self.client.release()

    def test_current_http_errors_without_semantic_codes_preserve_server_message(self):
        self.endpoint.failure = HTTPError('http://127.0.0.1/api/arm/state', 503, 'unavailable',
                                          protocol_headers(), io.BytesIO(b'{"error":"Transport failed"}'))
        with self.assertRaisesRegex(DevClientError, 'Transport failed') as error:
            self.client.arm_state()
        self.assertEqual(error.exception.status, 503)

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

    def test_duplicate_response_keys_including_nested_extensions_are_rejected(self):
        for body in (b'{"mode":"future","mode":"disabled"}',
                     b'{"mode":"disabled","extra":{"enabled":false,"enabled":true}}'):
            with self.subTest(body=body):
                self.client.acquire()
                self.endpoint.response = body
                try:
                    with self.assertRaises(DevClientError):
                        self.client.status()
                    self.assertIsNone(self.client.lease)
                finally:
                    self.endpoint.response = None
                    self.client.release()

    def test_status_accepts_complete_disabled_control_state(self):
        self.endpoint.response = control_status(mode='disabled')
        self.assertEqual(self.client.status(), control_status(mode='disabled'))

    def test_status_rejects_minimal_disabled_state_and_forgets_ownership(self):
        self.client.acquire()
        self.endpoint.response = {'mode': 'disabled', 'new': 'extension'}
        requests = len(self.endpoint.calls)
        with self.assertRaises(DevClientError):
            self.client.status()
        self.assertIsNone(self.client.lease)
        self.assertEqual(len(self.endpoint.calls), requests + 1)

    def test_status_rejects_missing_fields_and_unknown_modes(self):
        for reply in ({}, {'mode': 'simulation'}, {'mode': 'disabled', 'dog': {}},
                      control_status(mode='future')):
            with self.subTest(reply=reply):
                self.endpoint.response = reply
                with self.assertRaises(DevClientError):
                    self.client.status()

    def test_status_rejects_malformed_nested_fields(self):
        for path, value in (
            (('dog', 'phase'), 'future'), (('dog', 'velocity'), {'vx': 0, 'vy': 0}),
            (('dog', 'velocity', 'vx'), True), (('dog', 'velocity', 'vx'), 10 ** 400),
            (('dog', 'limits', 'ttl'), float('inf')), (('dog', 'stop_verified'), 0),
            (('arm', 'motion_enabled'), 1), (('arm', 'reason'), {}),
            (('arm', 'position_deg', 'b'), float('nan')), (('estop',), 0),
        ):
            with self.subTest(path=path, value=type(value)):
                reply = control_status()
                fields = reply
                for key in path[:-1]:
                    fields = fields[key]
                fields[path[-1]] = value
                self.endpoint.response = reply
                with self.assertRaises(DevClientError):
                    self.client.status()

    def test_acquire_rejects_a_valid_token_with_an_invalid_status(self):
        for reply in ({'lease': 'opaque-lease'}, {'lease': 'opaque-lease', 'status': {'mode': 'disabled'}},
                      {'lease': 'opaque-lease', 'status': dict(control_status(), dog={})}):
            with self.subTest(reply=reply):
                self.endpoint.response = reply
                try:
                    with self.assertRaises(DevClientError):
                        self.client.acquire()
                    self.assertIsNone(self.client.lease)
                finally:
                    self.endpoint.response = None
                    self.client.release()
        self.endpoint.response = None
        self.client.acquire()
        self.assertEqual(self.client.lease, 'opaque-lease')

    def test_control_commands_require_full_control_status(self):
        for method in (self.client.heartbeat, self.client.stand, self.client.velocity,
                       self.client.stop, self.client.release, self.client.estop,
                       lambda: self.client.reset('revision'), self.client.arm_stop,
                       lambda: self.client.arm_jog('b', 1)):
            with self.subTest(method=method.__name__):
                self.client.acquire()
                self.endpoint.response = {'mode': 'disabled'}
                requests = len(self.endpoint.calls)
                try:
                    with self.assertRaises(DevClientError):
                        method()
                    self.assertIsNone(self.client.lease)
                    self.assertEqual(len(self.endpoint.calls), requests + 1)
                finally:
                    self.endpoint.response = None
                    self.client.release()

    def test_status_accepts_additive_fields_in_nested_contracts(self):
        reply = control_status('hardware')
        reply['new_info'] = {'future': 1}
        reply['dog']['velocity']['new_axis'] = 1
        reply['arm']['capabilities'] = dict(arm_capabilities(), camera_feedback=True)
        reply['arm']['capabilities']['max_delta_deg']['new_axis'] = 1
        reply['arm']['operation'] = dict(arm_operation(), target_precision=0.1)
        reply['arm']['operation']['observed_start']['position_deg']['new_axis'] = 1
        self.endpoint.response = reply
        self.assertEqual(self.client.status(), reply)

    def test_arm_variants_accept_complete_replies(self):
        unobserved = dict(control_status()['arm'], observed=False, operation=None)
        for method, reply in (
            (self.client.arm_state, dict(arm_observation(), sample_monotonic=123.4)),
            (self.client.arm_state, unobserved), (self.client.arm_reconnect, unobserved),
        ):
            with self.subTest(method=method.__name__, observed=reply['observed']):
                self.endpoint.response = reply
                self.assertEqual(method(), reply)

    def test_arm_state_rejects_invalid_observed_and_unobserved_responses(self):
        without_gripper = arm_observation()
        del without_gripper['gripper_position_deg']
        for reply in (
            {}, without_gripper, dict(arm_observation(), observed=1),
            dict(arm_observation(), gripper_position_deg=0),
            dict(arm_observation(), position_deg={'e': 1}),
            dict(arm_observation(), sample_monotonic=False),
            dict(control_status()['arm'], observed=False, capabilities={}),
        ):
            with self.subTest(reply=reply):
                self.endpoint.response = reply
                with self.assertRaises(DevClientError):
                    self.client.arm_state()

    def test_arm_reconnect_rejects_invalid_variant(self):
        gateway = {'connected': False, 'reconnect_required': False,
                   'motion_enabled': False, 'reason': 'Reconnect scheduled'}
        for reply in ({}, arm_observation(), dict(gateway, connected=0),
                      dict(gateway, reconnect_required=True), dict(gateway, reason=None),
                      dict(control_status()['arm'], observed=False, position_deg={'e': True})):
            with self.subTest(reply=reply):
                self.endpoint.response = reply
                with self.assertRaises(DevClientError):
                    self.client.arm_reconnect()

    def test_arm_status_rejects_invalid_operation_and_capability_variants(self):
        for field, value in (
            ('operation', {}), ('operation', dict(arm_operation(), joint='w')),
            ('operation', dict(arm_operation(), hold_status='future')),
            ('capabilities', dict(arm_capabilities(), joints=['b', 'new'])),
            ('capabilities', dict(arm_capabilities(), max_delta_deg={})),
            ('capabilities', dict(arm_capabilities(), asynchronous=1)),
            ('capabilities', dict(arm_capabilities(), gripper_feedback=True)),
        ):
            with self.subTest(field=field, value=value):
                reply = control_status('hardware')
                reply['arm'][field] = value
                self.endpoint.response = reply
                with self.assertRaises(DevClientError):
                    self.client.status()

    def test_arm_operation_rejects_nested_values_outside_contract(self):
        for reply in (
            {}, dict(arm_operation(), phase='future'),
            dict(arm_operation(), speed_deg_s=float('inf')),
            dict(arm_operation(), observed_end={'position_deg': {'e': 1}}),
            dict(arm_operation(), targets_written=[dict(b=True, s=2, e=3, w=4, h=0)]),
            dict(arm_operation(), observed_stationary=0),
        ):
            with self.subTest(reply=reply):
                self.endpoint.response = reply
                with self.assertRaises(DevClientError):
                    self.client.arm_operation('operation-1')

    def test_explicit_v1_protocol_is_accepted(self):
        for versions in (('1',), (' \t1\t ',)):
            with self.subTest(versions=versions):
                self.endpoint.versions = versions
                self.assertEqual(self.client.status(), control_status())

    def test_missing_incompatible_and_ambiguous_protocol_fail_without_retry(self):
        for versions in ((), ('2',), ('',), ('1, 1',), ('1', '1'), ('1', '2')):
            with self.subTest(versions=versions):
                self.client.acquire()
                self.endpoint.versions = versions
                requests = len(self.endpoint.calls)
                try:
                    with self.assertRaises(DevClientError):
                        self.client.velocity(vx=0.1)
                    self.assertIsNone(self.client.lease)
                    self.assertEqual(len(self.endpoint.calls), requests + 1)
                finally:
                    self.endpoint.versions = ('1',)
                    self.client.release()

    def test_deep_success_reply_clears_its_originating_lease(self):
        self.client.acquire()
        self.endpoint.response = DEEP_JSON
        with self.assertRaises(DevClientError):
            self.client.heartbeat()
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
