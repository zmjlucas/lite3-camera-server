"""Offline SDK rules and guarded requests, without a robot connection."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import lite3_dev_server as sdk
import control
from test_dev_client import Response
from tests.test_control import Clock


class ServiceEndpoint:
    """Exercise the real control state machine through the client's HTTP boundary."""
    def __init__(self, service):
        self.service = service
        self.calls = []
        self.transform = lambda action, value: value

    def open(self, request, timeout):
        action = request.full_url.split('/api/', 1)[1]
        body = json.loads(request.data) if request.data else None
        self.calls.append((action, body))
        value = self.service.status() if action == 'status' else self.service.dispatch(action, body)
        return Response(self.transform(action, value))


class GuardedClientTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.service = control.ControlService(environment='dev', simulate=True, clock=self.clock)
        self.addCleanup(self.service.close)
        self.endpoint = ServiceEndpoint(self.service)
        patcher = patch(sdk.DevClient.__module__ + '.build_opener', return_value=self.endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, target='simulation'):
        return sdk.DevClient('http://127.0.0.1:8080', target=target)

    def prepare(self, client):
        client.stand()
        self.service._tick()
        for elapsed in (1.5, 1.5, 1.5, .5, .21, .21, .21):
            self.clock.advance(elapsed)
            client.heartbeat()
            self.service._tick()

    def test_target_mismatch_preflight_and_acquire_never_submit_control(self):
        client = self.client('hardware')
        result = client.preflight('acquire')
        self.assertFalse(result['ok'])
        with self.assertRaises(sdk.PreflightError) as error:
            client.acquire()
        self.assertFalse(error.exception.result['ok'])
        self.assertIsNone(error.exception.status)
        self.assertIsNone(error.exception.code)
        self.assertEqual(self.endpoint.calls, [('status', None), ('status', None)])
        self.assertIsNone(client.lease)

    def test_acquire_response_drift_releases_only_issued_lease(self):
        def drift(action, value):
            if action == 'control/acquire':
                value['status']['dog']['simulated'] = False
            return value
        self.endpoint.transform = drift
        client = self.client()
        with self.assertRaises(sdk.PreflightError):
            client.acquire()
        self.assertIsNone(client.lease)
        self.assertFalse(self.service.status()['lease_active'])
        self.assertEqual([action for action, _ in self.endpoint.calls],
                         ['status', 'control/acquire', 'control/release'])
        self.assertTrue(self.endpoint.calls[-1][1]['lease'])

    def test_runtime_limits_prevent_motion_posts_and_keep_sequence(self):
        client = self.client()
        client.acquire()
        self.prepare(client)
        with patch.dict(control.LIMITS, vx=.05):
            for call in (lambda: client.forward(.1, 1.), lambda: client.velocity(vx=.1)):
                before = len(self.endpoint.calls)
                with self.assertRaises(sdk.PreflightError):
                    call()
                self.assertEqual(self.endpoint.calls[before:], [('status', None)])
        for call in (lambda: client.forward(True), lambda: client.velocity(ttl=float('nan')),
                     lambda: client.arm_jog('e', 1, False)):
            before = len(self.endpoint.calls)
            with self.assertRaises(sdk.DevClientError):
                call()
            self.assertEqual(len(self.endpoint.calls), before)
        client.velocity(vx=.05)
        self.assertEqual(self.endpoint.calls[-1][1]['seq'], 2)

    def test_guarded_arm_target_action_submits_once_and_blocks_overlap(self):
        client = self.client()
        client.acquire()
        accepted = client.arm_action({'e': 12.}, duration=1.)
        operation = accepted['arm']['operation']
        self.assertEqual(operation['target_position_deg']['e'], 12.)
        blocked = client.preflight('arm/action')
        self.assertIn('arm_busy', [reason['code'] for reason in blocked['reasons']])
        with self.assertRaises(sdk.PreflightError):
            client.arm_action({'e': 18.}, duration=1.)
        submissions = [body for action, body in self.endpoint.calls if action == 'arm/action']
        self.assertEqual([body['targets'] for body in submissions], [{'e': 12.}])


if __name__ == '__main__':
    unittest.main()
