"""Exercise passive diagnostics without a robot, camera or serial device."""
import json
import os
from pathlib import Path
import tempfile
import shlex
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.application import ControlApplication
from src.camera import LatestFrame
from src.control import ControlService
from src.http_server import CameraRequestHandler
from src import launcher
from src.lite3_dev_server._build_info import UNKNOWN_BUILD
from fixtures.http_connection import MemoryConnection


class DiagnosticHTTPTests(unittest.TestCase):
    def request(self, service, frames, gateway=None, method='OPTIONS'):
        connection = MemoryConnection((method + ' /api/diagnostics HTTP/1.1\r\nHost: localhost\r\n\r\n').encode())
        host = SimpleNamespace(application=ControlApplication(service, gateway), frames=frames,
                               runtime_info={'version': {'sha256': 'startup-bundle'}},
                               build_info=UNKNOWN_BUILD)
        CameraRequestHandler(connection, ('127.0.0.1', 1), host)
        header, body = bytes(connection.output).split(b'\r\n\r\n', 1)
        self.assertEqual(int(header.split()[1]), 200)
        return json.loads(body)

    def test_diagnostic_read_preserves_expired_lease_and_camera_generation(self):
        now = [10.0]
        service = ControlService(environment='dev', simulate=True, clock=lambda: now[0])
        self.addCleanup(service.close)
        service.dispatch('control/acquire', {'owner': 'operator'})
        before = service.status()
        frames = LatestFrame('v4l2', source_id='active-camera')
        frames.publish(b'current-jpeg')
        now[0] += 10  # A normal status read expires this lease.
        result = self.request(service, frames)
        self.assertEqual(result['control'], before)
        self.assertEqual(result['runtime']['version']['sha256'], 'startup-bundle')
        self.assertEqual(result['camera']['source_id'], 'active-camera')
        health, frame, sequence = frames.snapshot()
        self.assertEqual((health['generation'], frame, sequence), (0, b'current-jpeg', 1))
        # A second read detects accidental mutations made while writing headers.
        self.assertEqual(self.request(service, frames, method='GET')['control'], before)
        self.assertFalse(service.status()['lease_active'])

    @unittest.skipUnless(os.name == 'posix', 'Commissioning requires POSIX identity')
    def test_diagnostic_read_preserves_unspent_commissioning_profile(self):
        from src.arm_motion import ArmMotionGateway
        from src.remote.arm_worker import SerialSession
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.json'
            profile = {'version': 1, 'machine_id': 'test-host', 'boot_id': 'test-boot',
                       'nonce': 'unused-profile', 'device': '/dev/testR5',
                       'targets_deg': dict(b=0, s=0, e=0, w=0, h=0),
                       'observed_deg': dict(b=0, s=0, e=0, w=0),
                       'gripper_reference': 'Offline fixture'}
            path.write_text(json.dumps(profile))
            path.chmod(0o600)
            original = path.read_bytes()
            gateway = ArmMotionGateway('/dev/testR5', path,
                                       identity_reader=lambda: ('test-host', 'test-boot'))
            service = ControlService(environment='dev', dog_enabled=False, arm_gateway=gateway)
            try:
                with patch.object(SerialSession, '_open', side_effect=AssertionError('serial opened')):
                    before = gateway.status()
                    result = self.request(service, LatestFrame('none'), gateway)
                    self.assertTrue(result['control']['arm']['motion_enabled'])
                    self.assertEqual(gateway.status(), before)
                    self.assertEqual(list(Path(directory).iterdir()), [path])
                    self.assertEqual(path.read_bytes(), original)
            finally:
                service.close()


class DoctorTransportTests(unittest.TestCase):

    @unittest.skipUnless(os.name == 'posix', 'Remote host runs Bash')
    def test_ssh_payload_runs_before_deployment_and_preserves_quoted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            unusual = str(Path(directory) / "missing ' $(touch SHOULD_NOT_EXIST)")
            args = launcher.parser().parse_args(['doctor', 'robot', '--direct', '--remote-port', '1',
                                                 '--install-dir', unusual, '--arm-device', unusual,
                                                 '--dog-state-port', '43901'])
            command = shlex.split(launcher.doctor_script(args))
            # Execute the exact remote runner without SSH or access to the source checkout.
            result = subprocess.run(command, cwd=directory, env=dict(os.environ, PYTHONPATH=''),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                    timeout=45)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('not_deployed', result.stdout)
            self.assertIn('"port": 43901', result.stdout)
            self.assertIn(unusual, result.stdout)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
