"""SDK contract checks at the HTTP boundary; no robot connections."""
import io
from http.client import HTTPMessage
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lite3_dev_server import DevClient, DevClientError


def protocol_headers(versions=('1',)):
    headers = HTTPMessage()
    for version in versions:
        headers.add_header('X-Lite3-Dev-Protocol', version)
    return headers


class Response(io.BytesIO):
    def __init__(self, body, versions=('1',)):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = protocol_headers(versions)


def control_status(mode='hardware', lease_active=False, environment='dev', simulated=True):
    return {
        'mode': mode,
        'environment': environment,
        'estop': False,
        'safety_revision': 'safety-revision',
        'lease_active': lease_active,
        'dog': {
            'phase': 'disarmed', 'velocity': {'vx': 0, 'vy': 0, 'yaw': 0},
            'limits': {'vx': 0.2, 'vy': 0.1, 'yaw': 0.3, 'ttl': 0.5},
            'error': None, 'stop_verified': False, 'simulated': simulated,
            'motion_enabled': simulated, 'actions_enabled': False,
            'feedback': {
                'observed': False, 'simulated': simulated, 'fresh': simulated,
                'age_s': 0.0 if simulated else None,
                'basic_state': 6 if simulated else None,
                'gait_state': 0 if simulated else None,
                'motion_state': 0 if simulated else None,
                'standing': simulated, 'motion_ready': simulated, 'reason': None,
            },
            'action': None,
        },
        'arm': {
            'motion_enabled': simulated, 'reason': 'Simulation' if simulated else None,
            'position_deg': dict(b=0, s=0, e=0, w=0, h=0) if simulated else None,
            'simulated': simulated,
        },
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

    def test_dog_action_lookup_requires_only_id_and_preserves_owned_sequence(self):
        record = action_status('completed', lease_active=False)['dog']['action']
        self.endpoint.response = dict(record, future_field='preserved')
        self.assertEqual(self.client.dog_action_status('action-1'), self.endpoint.response)
        self.assertIsNone(self.client.lease)
        request, body, _ = self.endpoint.calls[-1]
        self.assertEqual(request.full_url, 'http://127.0.0.1:8080/api/dog/action/status')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(body, {'id': 'action-1'})
        self.endpoint.response = None
        self.client.acquire()
        self.client.stand()
        for phase in ('running', 'completed', 'cancelled', 'fault'):
            self.endpoint.response = action_status(phase)['dog']['action']
            self.assertEqual(self.client.dog_action_status('action-1')['phase'], phase)
            self.assertEqual(self.client.lease, 'opaque-lease')
        self.endpoint.response = None
        self.client.velocity(vx=.1)
        self.assertEqual(self.endpoint.calls[-1][1]['seq'], 2)

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

    def test_stop_supersedes_a_delayed_motion_response(self):
        self.client.acquire()
        entered, proceed = threading.Event(), threading.Event()
        original = self.endpoint.open

        def delayed(request, timeout):
            response = original(request, timeout)
            if request.full_url.endswith('/dog/velocity'):
                entered.set()
                if not proceed.wait(3):
                    raise AssertionError('Test did not release its delayed response')
            return response

        self.endpoint.open = delayed
        self.addCleanup(proceed.set)
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

    def test_error_clears_lease_and_never_retries(self):
        self.client.acquire()
        self.endpoint.failure = HTTPError('http://127.0.0.1/api/dog/velocity', 409, 'conflict', protocol_headers(),
                                          io.BytesIO(b'{"error":"lease expired","code":"lease_required"}'))
        with self.assertRaisesRegex(DevClientError, 'lease expired') as error:
            self.client.velocity(vx=0.1)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(getattr(error.exception, 'code', None), 'lease_required')
        self.assertIsNone(self.client.lease)
        self.assertEqual(len(self.endpoint.calls), 2)
        with self.assertRaises(DevClientError):
            self.client.stand()
        self.assertEqual(len(self.endpoint.calls), 2)

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

if __name__ == '__main__':
    unittest.main()
