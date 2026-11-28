"""Exercise managed execution through loopback HTTP and synthetic controllers."""
import os
import socket
import threading
import unittest
from unittest.mock import patch

import test_arm_integration as arm_integration
from src.camera import LatestFrame
from src.control import ControlService
from src.http_server import CameraHTTPServer
from src.lite3_dev_server import ArmJog, DogAction, ManagedClient


class DispatchAudit:
    def __init__(self, control):
        self._lock = threading.Lock()
        self._calls = []
        self.leases = []
        original = control.dispatch

        def dispatch(action, body):
            with self._lock:
                self._calls.append((action, dict(body)))
            result = original(action, body)
            if action == 'control/acquire':
                with self._lock:
                    self.leases.append(result['lease'])
            return result

        control.dispatch = dispatch

    def calls(self, action=None):
        with self._lock:
            return [(name, dict(body)) for name, body in self._calls
                    if action is None or name == action]


def forbid_udp(test):
    """Allow the loopback HTTP transport while rejecting robot UDP sockets."""
    original = socket.socket
    attempts = []

    def create(*args, **kwargs):
        kind = kwargs.get('type', args[1] if len(args) > 1 else socket.SOCK_STREAM)
        if kind == socket.SOCK_DGRAM:
            attempts.append(kind)
            raise AssertionError('Offline managed tests must never open robot UDP sockets')
        return original(*args, **kwargs)

    guard = patch('socket.socket', side_effect=create)
    guard.start()
    test.addCleanup(guard.stop)
    test.addCleanup(lambda: test.assertEqual(attempts, []))


class ManagedDogIntegrationTests(unittest.TestCase):
    def setUp(self):
        forbid_udp(self)
        self.control = ControlService(environment='dev', simulate=True, dog_host='127.0.0.1')
        self.addCleanup(self.control.close)
        self.audit = DispatchAudit(self.control)
        self.server = CameraHTTPServer(0, LatestFrame('none'), bind='127.0.0.1', control=self.control)
        self.addCleanup(self.server.server_close)
        worker = threading.Thread(target=self.server.serve_forever,
                                  kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.shutdown)
        self.control.start()
        self.url = 'http://127.0.0.1:{}'.format(self.server.server_port)
        self.managed = ManagedClient(self.url)
        self.addCleanup(self.managed.close)

    def test_two_dog_actions_renew_prepare_and_reacquire_through_http(self):
        result = self.managed.run_sequence(
            [DogAction('forward', speed=.1, duration=.1),
             DogAction('turn_left', speed=.2, duration=.1)],
            policy='completed_only', timeout=30)

        self.assertEqual(result.phase, 'completed', result)
        self.assertIsInstance(result.results, tuple)
        self.assertEqual(len(result.results), 2)
        self.assertEqual([item.phase for item in result.results], ['completed', 'completed'])
        self.assertEqual(len({item.action_id for item in result.results}), 2)
        for item in result.results:
            self.assertTrue(item.action_id)
            self.assertEqual(item.status['dog']['action']['id'], item.action_id)
            self.assertEqual(item.status['dog']['action']['phase'], 'completed')
            self.assertFalse(item.status['lease_active'])
            self.assertFalse(item.status['dog']['stop_verified'])

        self.assertEqual(len(self.audit.leases), 2)
        self.assertEqual(len(set(self.audit.leases)), 2)
        stands = self.audit.calls('dog/stand')
        submissions = self.audit.calls('dog/action')
        self.assertEqual(len(stands), 2)
        self.assertEqual(len(submissions), 2)
        for lease, (_, stand), (_, action) in zip(self.audit.leases, stands, submissions):
            self.assertEqual(stand['lease'], lease)
            self.assertEqual(action['lease'], lease)
            self.assertLess(stand['seq'], action['seq'])
            renewed = [body for _, body in self.audit.calls('control/heartbeat')
                       if body['lease'] == lease]
            self.assertGreaterEqual(len(renewed), 2)
        self.assertEqual(self.control.status()['dog']['phase'], 'disarmed')
        self.assertFalse(self.control.status()['lease_active'])
        self.assertIsNone(self.control._socket)

@unittest.skipUnless(os.name == 'posix', 'Bounded synthetic serial writes require POSIX')
class ManagedArmIntegrationTests(unittest.TestCase):
    def setUp(self):
        forbid_udp(self)
        # Reuse the loopback/pipe setup without inheriting the low-level SDK tests.
        arm_integration.ArmIntegrationTests.setUp(self)
        self.audit = DispatchAudit(self.control)
        self.managed = ManagedClient('http://127.0.0.1:{}'.format(self.server.server_port))
        self.addCleanup(self.managed.close)

    def test_arm_completion_and_unverified_gripper_stop_sequence(self):
        result = self.managed.run_sequence(
            [ArmJog('e', 2., speed=1.), ArmJog('h', .5, speed=1.), ArmJog('b', .5, speed=1.)],
            policy='completed_only', timeout=20)

        self.assertEqual(result.phase, 'commanded_unverified', result)
        self.assertEqual([item.phase for item in result.results], ['completed', 'commanded_unverified'])
        self.assertEqual(len({item.action_id for item in result.results}), 2)
        for item in result.results:
            self.assertEqual(item.operation['operation_id'], item.action_id)
            self.assertEqual(item.operation['phase'], item.phase)
            self.assertTrue(item.operation['observed_stationary'])
            self.assertFalse(item.operation['stop_verified'])
            self.assertIsNone(item.operation['gripper_position_deg'])
        self.assertEqual(len(result.results[1].operation['targets_written']), 1)
        self.assertEqual([body['joint'] for _, body in self.audit.calls('arm/jog')], ['e', 'h'])
        self.assertEqual(len(self.audit.leases), 2)
        self.assertEqual(len(set(self.audit.leases)), 2)
        self.assertGreaterEqual(len(self.audit.calls('control/heartbeat')), 2)
        self.board.drain()
        self.assertGreater(len(self.board.motions), 2)
        self.assertEqual(self.board.motions[-1]['e'], 10.)
        self.assertEqual(self.board.motions[-1]['h'], -15.5)
        for packet in self.board.motions:
            self.assertEqual({axis: packet[axis] for axis in ('b', 's', 'w')},
                             {'b': 10., 's': -4., 'w': 6.})
        self.assertFalse(self.control.status()['lease_active'])
        self.assertIsNone(self.control._socket)

if __name__ == '__main__':
    unittest.main()
