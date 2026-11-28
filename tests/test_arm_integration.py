"""Run SDK/HTTP/ownership/serial framing together against an offline controller."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from src.arm_motion import ArmMotionGateway
from src.control import ControlService
from src.lite3_dev_server import DevClient
from src.remote.arm_worker import SerialSession
from src.camera import LatestFrame
from src.http_server import CameraHTTPServer
from test_arm_motion import Controller


@unittest.skipUnless(os.name == 'posix', 'Bounded serial writes require POSIX')
class ArmIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        targets = {'b': 10., 's': -4., 'e': 8., 'w': 6., 'h': -16.}
        observed = {'b': 10.1, 's': -4.2, 'e': 11.69, 'w': 6.05}
        record = {'version': 1, 'machine_id': 'offline-host', 'boot_id': 'offline-boot',
                  'nonce': 'offline-integration', 'device': '/dev/testR5',
                  'targets_deg': targets, 'observed_deg': observed,
                  'gripper_reference': 'Synthetic controller; never use on hardware'}
        path = Path(temp.name) / 'commission.json'
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        self.board = Controller(targets, observed)
        self.addCleanup(self.board.cleanup)
        def factory(device):
            session = SerialSession(device, timeout=.05)
            session._open = lambda: setattr(session, '_port', self.board)
            return session
        self.gateway = ArmMotionGateway('/dev/testR5', str(path), session_factory=factory,
                                         identity_reader=lambda: ('offline-host', 'offline-boot'))
        self.addCleanup(self.gateway.close)
        self.control = ControlService(mode='hardware', environment='dev', dog_host='127.0.0.1',
                                      arm_gateway=self.gateway, dog_enabled=False)
        self.addCleanup(self.control.close)
        self.server = CameraHTTPServer(0, LatestFrame('none'), bind='127.0.0.1',
                                      control=self.control, arm_gateway=self.gateway)
        self.addCleanup(self.server.server_close)
        worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.shutdown)
        self.control.start()
        self.client = DevClient('http://127.0.0.1:{}'.format(self.server.server_port), timeout=.5)
        self.addCleanup(self.client.close)

    def test_two_degree_jog_renews_lease_and_preserves_other_targets(self):
        self.client.acquire('offline-integration')
        accepted = self.client.arm_jog('e', 2., speed=1.)
        op_id = accepted['arm']['operation']['operation_id']
        deadline = time.monotonic() + 8
        while True:
            operation = self.client.arm_operation(op_id)
            if operation['phase'] not in ('preflight', 'moving', 'stopping'):
                break
            self.assertLess(time.monotonic(), deadline, operation)
            self.client.heartbeat()
            time.sleep(.15)
        self.assertEqual(operation['phase'], 'completed', operation)
        self.assertTrue(operation['observed_stationary'])
        self.assertFalse(operation['stop_verified'])
        self.board.drain()
        self.assertGreater(len(self.board.motions), 1)
        self.assertEqual(self.board.motions[-1]['e'], 10.)
        for packet in self.board.motions:
            self.assertEqual({axis: packet[axis] for axis in ('b', 's', 'w', 'h')},
                             {'b': 10., 's': -4., 'w': 6., 'h': -16.})
        state = self.client.arm_state()
        self.assertAlmostEqual(state['position_deg']['e'], 13.69)
        self.assertIsNone(state['gripper_position_deg'])
        self.assertIsNone(self.control._socket)
        self.client.estop()
        self.assertFalse(self.client.arm_state()['motion_enabled'])

    def test_http_stop_cancels_preflight_without_motion_and_releases_lease(self):
        self.client.acquire()
        result = self.client.arm_jog('e', .5, speed=1.)
        op_id = result['arm']['operation']['operation_id']
        stopped = self.client.arm_stop()
        self.assertFalse(stopped['lease_active'])
        deadline = time.monotonic() + 2
        while self.client.arm_operation(op_id)['phase'] == 'stopping':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.01)
        self.assertEqual(self.client.arm_operation(op_id)['phase'], 'cancelled')
        self.board.drain()
        self.assertEqual(self.board.motions, [])
        self.assertIsNone(self.client.lease)
        self.assertIsNone(self.control._socket)

    def test_base_then_gripper_keeps_gripper_observation_unknown(self):
        self.client.acquire('offline-staged-diagnostic')
        for joint, phase in (('b', 'completed'), ('h', 'commanded_unverified')):
            reply = self.client.arm_jog(joint, .5, speed=1.)
            op_id = reply['arm']['operation']['operation_id']
            deadline = time.monotonic() + 5
            while True:
                operation = self.client.arm_operation(op_id)
                if operation['phase'] not in ('preflight', 'moving', 'stopping'):
                    break
                self.assertLess(time.monotonic(), deadline, operation)
                self.client.heartbeat()
                time.sleep(.15)
            self.assertEqual(operation['phase'], phase, operation)
            self.assertTrue(operation['observed_stationary'])
            self.assertIsNone(operation['gripper_position_deg'])
            self.assertFalse(operation['stop_verified'])
            if joint == 'h':
                self.assertEqual(len(operation['targets_written']), 1)
        self.board.drain()
        self.assertEqual(self.board.motions[-1]['b'], 10.5)
        self.assertEqual(self.board.motions[-1]['h'], -15.5)
        observed = self.client.arm_state()
        self.assertAlmostEqual(observed['position_deg']['b'], 10.6)
        self.assertIsNone(observed['gripper_position_deg'])
        self.assertIsNone(self.control._socket)


if __name__ == '__main__':
    unittest.main()
