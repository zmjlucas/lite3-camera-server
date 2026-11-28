"""Exercise passive diagnostics without a robot, camera or serial device."""
import json
import io
import os
from pathlib import Path
import tempfile
import shlex
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.application import ControlApplication
from src.camera import LatestFrame
from src.control import ControlService
from src.http_server import CameraRequestHandler
from src import launcher
from src.lite3_dev_server._build_info import UNKNOWN_BUILD
from src.remote import camera_doctor as doctor
from src.runtime_info import runtime_identity
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


class RuntimeIdentityTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'POSIX FIFO metadata')
    def test_version_inspection_skips_nonregular_files_without_opening_them(self):
        from src import runtime_info
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / 'control.py'
            os.mkfifo(str(fifo))
            original = Path.read_bytes
            def read_file(path):
                if path == fifo:
                    raise AssertionError('Opened a device-like bundle entry')
                return original(path)
            with patch.object(Path, 'read_bytes', read_file):
                version = runtime_info.bundle_version(directory)
            self.assertIsNone(version['sha256'])
            self.assertIn('control.py', version['unavailable'])

    def test_bundle_identity_tracks_installed_files_but_keeps_startup_version(self):
        from src import runtime_info
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source, folder in launcher.SERVER_BUNDLE:
                destination = root / Path(folder).relative_to('lite3-dev-server') / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            runtime = runtime_info.runtime_identity(SimpleNamespace(dog_state_port=43901), root)
            original = runtime['version']['sha256']
            self.assertEqual(len(original), 64)
            self.assertEqual(runtime['config']['dog_state_port'], 43901)
            (root / 'control.py').write_text('# new deployment\n')
            self.assertNotEqual(runtime_info.bundle_version(root)['sha256'], original)
            self.assertEqual(runtime['version']['sha256'], original)
            (root / 'web/control.html').unlink()
            incomplete = runtime_info.bundle_version(root)
            self.assertIsNone(incomplete['sha256'])
            self.assertIn('web/control.html', incomplete['unavailable'])


class DoctorReportTests(unittest.TestCase):
    def report(self, options=None, runtime=None, sources=None, owners=None):
        control = {'mode': 'hardware', 'dog': {'simulated': False,
                   'feedback': {'observed': True, 'fresh': True, 'age_s': 0.1}}, 'arm': {}}
        snapshot = {'schema': 1, 'runtime': runtime, 'control': control, 'camera': {}}
        with patch.object(doctor, 'service_status', return_value={'state': 'unverified', 'properties': {}}), \
                patch.object(doctor, 'fetch_diagnostics', return_value={'state': 'observed', 'snapshot': snapshot}), \
                patch.object(doctor.camera_sources, 'discover_sources', return_value=sources or []), \
                patch.object(doctor, 'device_owners', return_value=owners or {'devices': {}, 'scope': 'visible_processes_only'}), \
                patch.object(doctor, 'run', return_value={'state': 'unverified', 'output': 'tool missing'}):
            return doctor.collect_report(options or {})

    def test_http_peer_cannot_select_dependency_executable_or_installation_directory(self):
        runtime = runtime_identity(SimpleNamespace(), Path('/missing-bundle'))
        runtime.update(python='/untrusted/executable', directory='/untrusted/files')
        report = self.report(runtime=runtime)
        self.assertEqual(report['dependencies']['python'], sys.executable)
        self.assertEqual(report['installed']['directory'], str(Path.home() / 'lite3-dev-server'))

    def test_inspection_override_keeps_feedback_bound_to_actual_receiver(self):
        runtime = runtime_identity(SimpleNamespace(dog_host='192.168.1.120', dog_state_port=43897))
        report = self.report({'dog_host': '192.168.1.121', 'dog_state_port': 43901}, runtime)
        self.assertEqual(report['udp']['host'], '192.168.1.120')
        self.assertEqual(report['udp']['port'], 43897)
        self.assertEqual(report['udp']['inspection']['host'], '192.168.1.121')
        self.assertEqual(report['udp']['inspection']['port'], 43901)
        self.assertEqual(report['udp']['state'], 'feedback_fresh')

    def test_unreadable_installation_does_not_abort_other_checks(self):
        original = Path.stat
        unavailable = str(Path(tempfile.gettempdir()) / 'unreadable-installation')
        def restricted(path, *args, **kwargs):
            if str(path).startswith(unavailable):
                raise PermissionError('fixture traversal denied')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'stat', restricted):
            report = self.report({'install_dir': unavailable})
        self.assertEqual(report['installed']['state'], 'unverified')
        self.assertEqual(report['udp']['state'], 'feedback_fresh')
        self.assertIn('serial', report['dependencies']['modules'])

    def test_camera_ownership_includes_usb_and_sibling_interfaces(self):
        runtime = runtime_identity(SimpleNamespace())
        runtime['pid'] = 123
        related = ['/dev/video4', '/dev/video2', '/dev/bus/usb/002/003']
        source = {'device': '/dev/video4', 'related_devices': related, 'available': False,
                  'error': 'Camera is in use by service (PID 123). Release it in that application, then Refresh.'}
        resolved = [os.path.realpath(path) for path in related]
        owners = {'devices': {resolved[0]: [], resolved[1]: [], resolved[2]: [{'pid': 123, 'name': 'service'}]}}
        report = self.report(runtime=runtime, sources=[dict(source)], owners=owners)
        self.assertEqual(report['camera']['sources'][0]['occupancy'], 'running_service')
        self.assertNotIn('Release', report['camera']['sources'][0]['error'])
        owners['devices'][resolved[1]].append({'pid': 456, 'name': 'competing'})
        report = self.report(runtime=runtime, sources=[dict(source)], owners=owners)
        self.assertEqual(report['camera']['sources'][0]['occupancy'], 'other_process')
        self.assertEqual({owner['pid'] for owner in report['camera']['sources'][0]['owners']}, {123, 456})

    def test_feedback_conclusions_require_observed_evidence(self):
        for feedback, expected in (
                ({'observed': False, 'reason': 'Waiting for dog state feedback'}, 'feedback_missing'),
                ({'observed': True, 'fresh': False, 'age_s': 2.0}, 'feedback_stale'),
                ({'observed': True, 'fresh': True, 'age_s': 0.1}, 'feedback_fresh'),
                ({'observed': False, 'reason': 'Invalid dog state feedback received'}, 'invalid_feedback'),
                ({'observed': False, 'reason': 'Dog state receiver could not bind UDP port 43897: '
                  '[Errno 98] Address already in use'}, 'port_conflict'),
                ({'observed': False, 'reason': 'Dog state receiver could not bind UDP port 43897: '
                  '[Errno 13] Permission denied'}, 'bind_failed'),
                ({}, 'unverified')):
            with self.subTest(expected=expected):
                state = {'mode': 'hardware', 'dog': {'simulated': False, 'feedback': feedback}, 'arm': {}}
                self.assertEqual(doctor.feedback_report(state)['state'], expected)
        for state in ({'mode': 'readonly'}, {'mode': 'disabled'},
                      {'mode': 'hardware', 'dog': {'simulated': True}},
                      {'mode': 'hardware', 'arm': {'capabilities': {'joints': ['b']}}}):
            self.assertEqual(doctor.feedback_report(state)['state'], 'not_applicable')
        self.assertEqual(doctor.feedback_report(None)['state'], 'unverified')

    @unittest.skipUnless(os.name == 'posix', 'POSIX mode bits and symlinks')
    def test_device_permissions_use_service_groups_and_traversal_without_open(self):
        with tempfile.TemporaryDirectory(dir='/tmp' if os.name == 'posix' else None) as directory:
            root = Path(directory)
            device = root / 'ttyUSB0'
            device.write_bytes(b'preserve serial state')
            device.chmod(0o660)
            link = root / 'serial-by-id'
            link.symlink_to(device)
            account = {'uid': os.stat(device).st_uid + 1, 'gid': 99999,
                       'groups': [os.stat(device).st_gid]}
            root.chmod(0o755)
            with patch.object(os, 'open', side_effect=AssertionError('device opened')):
                allowed = doctor.inspect_device(str(link), account)
                self.assertEqual(allowed['mode_access'], 'read_write')
                self.assertEqual(allowed['resolved'], str(device.resolve()))
                account['groups'] = []
                self.assertEqual(doctor.inspect_device(str(link), account)['mode_access'], 'denied')
                account['groups'] = [os.stat(device).st_gid]
                root.chmod(0o700)
                self.assertEqual(doctor.inspect_device(str(link), account)['mode_access'], 'denied')
                self.assertEqual(doctor.inspect_device(str(root / 'missing'), account)['state'], 'missing')
            self.assertEqual(device.read_bytes(), b'preserve serial state')

    @unittest.skipUnless(os.name == 'posix', 'Linux procfs descriptor symlinks')
    def test_ownership_scan_reports_visible_owner_without_claiming_exclusive_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fd = root / '123/fd'
            fd.mkdir(parents=True)
            (root / '123/comm').write_text('robot-service\n')
            (fd / '7').symlink_to('/dev/ttyUSB0')
            result = doctor.device_owners(['/dev/ttyUSB0', '/dev/video9'], root)
            self.assertEqual(result['devices']['/dev/ttyUSB0'], [{'pid': 123, 'name': 'robot-service'}])
            self.assertEqual(result['devices']['/dev/video9'], [])
            self.assertEqual(result['scope'], 'visible_processes_only')
            (root / '123/comm').write_bytes(b'robot-\xff\n')
            result = doctor.device_owners(['/dev/ttyUSB0'], root)
            self.assertEqual(result['devices']['/dev/ttyUSB0'][0]['name'], 'robot-\ufffd')

    def test_missing_installation_keeps_other_checks_and_unknown_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            options = {'install_dir': directory, 'arm_device': str(Path(directory) / 'missing')}
            with patch.object(doctor, 'service_status', return_value={'state': 'unverified', 'properties': {}}), \
                    patch.object(doctor, 'fetch_diagnostics', return_value={'state': 'unverified', 'error': 'refused'}), \
                    patch.object(doctor.camera_sources, 'discover_sources', return_value=[]), \
                    patch.object(doctor, 'run', return_value={'state': 'unverified', 'output': 'tool missing'}):
                report = doctor.collect_report(options)
            self.assertEqual(report['installed']['state'], 'not_deployed')
            self.assertEqual(report['udp']['state'], 'unverified')
            self.assertEqual(report['serial']['device']['state'], 'missing')
            self.assertEqual(report['identity']['state'], 'unverified')
            self.assertIn('serial', report['dependencies']['modules'])
            self.assertIn('upload', doctor.render_report(report).lower())


class DoctorTransportTests(unittest.TestCase):
    def test_legacy_unknown_endpoint_preserves_expired_control_lease(self):
        from http.server import BaseHTTPRequestHandler
        now = [10.0]
        service = ControlService(environment='dev', simulate=True, clock=lambda: now[0])
        self.addCleanup(service.close)
        service.dispatch('control/acquire', {'owner': 'operator'})
        now[0] += 10
        class LegacyHandler(BaseHTTPRequestHandler):
            log_message = CameraRequestHandler.log_message
            # Legacy GET responses call status even for a missing route.
            def do_GET(self):
                service.status()
                self.send_error(404)
        class Transport:
            def __init__(self, *args, **kwargs):
                pass
            def request(self, method, path):
                conn = MemoryConnection(('{} {} HTTP/1.1\r\nHost: localhost\r\n\r\n'.format(method, path)).encode())
                LegacyHandler(conn, ('127.0.0.1', 1), SimpleNamespace())
                self.output = bytes(conn.output)
            def makefile(self, *args):
                return io.BytesIO(self.output)
            def getresponse(self):
                response = doctor.http.client.HTTPResponse(self)
                response.begin()
                return response
            def close(self):
                pass
        with patch.object(doctor.http.client, 'HTTPConnection', Transport):
            self.assertEqual(doctor.fetch_diagnostics(8080)['state'], 'unverified')
        self.assertTrue(service.snapshot()['lease_active'])

    def test_windows_doctor_command_fits_createprocess_limit(self):
        args = launcher.parser().parse_args(['doctor', 'ysc@192.168.1.103'])
        command = launcher.ssh_command(args.target, launcher.doctor_script(args))
        self.assertLess(len(subprocess.list2cmdline(command)), 32767)

    def test_http_probe_rejects_foreign_redirected_and_malformed_evidence(self):
        class Socket:
            def makefile(self, *args):
                return io.BytesIO(raw)
        cases = (
            (302, '1', b'{}'), (200, None, b'{}'), (404, '1', b'{}'),
            (200, '1', b'invalid JSON'),
            (200, '1', b'[' * 2000 + b']' * 2000),
            (200, '1', json.dumps({'schema': 1, 'control': {'dog': []}, 'camera': {},
                                  'runtime': {'config': []}}).encode()),
            (200, '1', json.dumps({'schema': 1, 'control': {'mode': 'hardware', 'dog': {'feedback': {}},
                                                          'arm': {}}, 'camera': {},
                                  'runtime': {'directory': '/bad\0path'}}).encode()),
        )
        for status, protocol, body in cases:
            with self.subTest(status=status, protocol=protocol, body=body):
                raw = ('HTTP/1.1 {} response\r\nContent-Length: {}\r\nLocation: http://example.invalid/\r\n'.format(
                    status, len(body))).encode()
                if protocol:
                    raw += ('X-Lite3-Dev-Protocol: ' + protocol + '\r\n').encode()
                raw += b'\r\n' + body
                response = doctor.http.client.HTTPResponse(Socket())
                response.begin()
                with patch.object(doctor.http.client, 'HTTPConnection') as connection:
                    connection.return_value.getresponse.return_value = response
                    result = doctor.fetch_diagnostics(8087)
                    self.assertEqual(result['state'], 'unverified')
                    connection.return_value.request.assert_called_once_with('OPTIONS', '/api/diagnostics')

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
