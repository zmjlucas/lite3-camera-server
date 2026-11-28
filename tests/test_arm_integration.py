"""Run SDK/HTTP/ownership/serial framing together against an offline controller."""
import json
import os
from pathlib import Path
import tempfile
import threading
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
        self.profile_path = path
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

    def test_status_is_passive_before_and_after_first_live_sample(self):
        for _ in range(3):
            arm = self.client.status()['arm']
            self.assertIn('connected', arm)
            self.assertFalse(arm['connected'])
            self.assertFalse(arm['reconnect_required'])
            self.assertEqual(arm['commissioning']['state'], 'pending_preflight')
            self.assertFalse(arm['commissioning']['nonce_consumed'])
            self.assertIsNone(arm['cached_feedback'])
            self.assertIsNone(arm['position_deg'])
        self.assertEqual(list(self.profile_path.parent.glob('.r5-used-*')), [])
        self.assertEqual(self.board.requests, [])
        observed = self.client.arm_state()
        arm = self.client.status()['arm']
        self.assertTrue(arm['connected'])
        self.assertTrue(arm['commissioning']['nonce_consumed'])
        self.assertEqual(arm['cached_feedback']['position_deg'], observed['position_deg'])
        self.assertEqual(arm['cached_feedback']['source'], 'local_serial')
        self.assertIsNone(arm['position_deg'])
        self.assertEqual(self.board.requests, [b'{"T":105}\n'])
        self.assertEqual(len(list(self.profile_path.parent.glob('.r5-used-*'))), 1)


if __name__ == '__main__':
    unittest.main()
