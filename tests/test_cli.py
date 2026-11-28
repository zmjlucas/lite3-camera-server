"""Offline CLI contract: validation, previews and machine-readable errors."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from test_dev_client import Response, action_status, arm_capabilities, arm_operation, control_status, protocol_headers


class CLIOfflineTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('src.lite3_dev_server.cli'),
                             'The SDK must include its control CLI')
        self.cli = importlib.import_module('src.lite3_dev_server.cli')

    def invoke(self, arguments):
        output, diagnostic = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostic):
            with patch('socket.socket', side_effect=AssertionError('Offline command used network')):
                code = self.cli.main(arguments)
        return code, output.getvalue(), diagnostic.getvalue()

    def test_default_dry_run_only_plans_status(self):
        code, output, diagnostic = self.invoke(['--dry-run'])
        self.assertEqual(code, 0, diagnostic)
        result = json.loads(output)
        self.assertTrue(result['ok'])
        self.assertEqual(result['command'], 'status')
        self.assertEqual(result['result']['requests'], [{'method': 'GET', 'path': '/api/status'}])
        self.assertEqual(diagnostic, '')

    def test_dog_preview_has_validated_parameters_and_explicit_preparation(self):
        code, output, diagnostic = self.invoke([
            '--url', 'http://example.invalid:8080', '--transport', 'simulated',
            'dog', 'forward', '--speed', '.1', '--duration', '1', '--dry-run'])
        self.assertEqual(code, 0, diagnostic)
        result = json.loads(output)['result']
        requests = result['requests']
        paths = [request['path'] for request in requests]
        self.assertLess(paths.index('/api/control/acquire'), paths.index('/api/dog/stand'))
        self.assertLess(paths.index('/api/dog/stand'), paths.index('/api/dog/action'))
        action = requests[paths.index('/api/dog/action')]
        self.assertEqual({key: action['body'][key] for key in ('name', 'speed', 'duration')},
                         {'name': 'forward', 'speed': .1, 'duration': 1.})
        self.assertEqual(result['url'], 'http://example.invalid:8080')
        self.assertTrue(result['dry_run'])

    def test_all_read_and_control_commands_have_offline_previews(self):
        cases = [(['arm', 'state'], '/api/arm/state'),
                 (['arm', 'reconnect'], '/api/arm/reconnect'),
                 (['arm', 'operation', 'operation-id'], '/api/arm/operation'),
                 (['arm', 'stop'], '/api/arm/stop'),
                 (['dog', 'stop'], '/api/dog/stop'),
                 (['estop'], '/api/control/estop'),
                 (['reset', '--revision', 'observed', '--transport', 'real'], '/api/control/reset'),
                 (['arm', 'jog', 'e', '--delta', '1', '--speed', '1',
                   '--transport', 'real'], '/api/arm/jog')]
        for arguments, path in cases:
            with self.subTest(arguments=arguments):
                code, output, diagnostic = self.invoke(arguments + ['--dry-run'])
                self.assertEqual(code, 0, diagnostic)
                self.assertIn(path, [r['path'] for r in json.loads(output)['result']['requests']])

    def test_mutation_requires_full_execute_flag_at_every_parser_level(self):
        command = ['dog', 'forward', '--speed', '.1', '--duration', '1', '--transport', 'simulated']
        cases = [command, ['--exec'] + command, ['dog', '--exec'] + command[1:],
                 command + ['--exec'], command + ['--execut'],
                 ['arm', 'jog', 'e', '--delta', '1', '--speed', '1', '--transport', 'simulated'],
                 ['reset', '--revision', 'observed', '--transport', 'simulated']]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assert_argument_error(arguments)

    def assert_argument_error(self, arguments):
        code, output, diagnostic = self.invoke(arguments)
        self.assertEqual(code, 2, (arguments, output, diagnostic))
        result = json.loads(output)
        self.assertFalse(result['ok'])
        self.assertEqual(result['kind'], 'argument_error')
        self.assertTrue(result['error'])
        self.assertTrue(diagnostic)
        self.assertNotIn('Traceback', diagnostic)

    def test_invalid_values_fail_before_network(self):
        for value in ('nan', 'inf', '-inf', '0', '-1', '.21'):
            with self.subTest(speed=value):
                self.assert_argument_error(['dog', 'forward', '--speed=' + value, '--duration', '1',
                                            '--transport', 'simulated', '--dry-run'])
        for value in ('nan', 'inf', '0', '-1', '10.1'):
            with self.subTest(duration=value):
                self.assert_argument_error(['dog', 'turn_left', '--speed', '.3', '--duration=' + value,
                                            '--transport', 'simulated', '--dry-run'])
        for arguments in (
                ['dog'], ['arm'], ['wat'],
                ['dog', 'forward', '--speed', '.1', '--duration', '1', '--execute'],
                ['arm', 'jog', 's', '--delta', '1', '--speed', '1', '--transport', 'real', '--dry-run'],
                ['arm', 'jog', 'b', '--delta', '1', '--speed', '1', '--transport', 'real', '--dry-run'],
                ['arm', 'jog', 'e', '--delta', '.01', '--speed', '1', '--transport', 'real', '--dry-run'],
                ['arm', 'jog', 'e', '--delta', '2', '--speed', '.1', '--transport', 'real', '--dry-run'],
                ['--timeout', 'nan', '--dry-run'], ['--wait-timeout', '0', '--dry-run'],
                ['--url', 'http://robot:bad', '--dry-run'],
                ['--url', 'http://robot:0', '--dry-run'],
                ['--url', 'http://robot/path', '--dry-run'],
                ['--url', 'http://user:password@robot', '--dry-run'],
                ['--url', 'http://robot\n.invalid', '--dry-run']):
            with self.subTest(arguments=arguments):
                self.assert_argument_error(arguments)

    def test_help_and_module_entrypoint_work_without_a_server(self):
        code, output, diagnostic = self.invoke(['--help'])
        self.assertEqual(code, 0)
        self.assertIn('lite3-control', output)
        self.assertIn('--execute', output)
        self.assertEqual(diagnostic, '')
        process = subprocess.run([sys.executable, '-B', '-m', 'lite3_dev_server', '--help'],
                                 cwd=str(Path(__file__).resolve().parents[1] / 'src'),
                                 capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('lite3-control', process.stdout)

    def test_malformed_origins_fail_offline_instead_of_changing_request_path(self):
        for url in ('http://robot#', 'http://robot?', 'http://robot\\name', 'http://robot\x7f'):
            with self.subTest(url=url):
                self.assert_argument_error(['--url', url, '--dry-run'])

    def test_jog_preview_matches_the_executed_request_sequence(self):
        for transport, expected in (
                ('simulated', ['/api/status', '/api/control/acquire', '/api/arm/jog', '/api/control/release']),
                ('real', ['/api/status', '/api/control/acquire', '/api/arm/jog',
                          '/api/control/heartbeat', '/api/arm/operation', '/api/control/release'])):
            with self.subTest(transport=transport):
                code, output, diagnostic = self.invoke([
                    'arm', 'jog', 'e', '--delta', '1', '--speed', '1', '--transport', transport, '--dry-run'])
                self.assertEqual(code, 0, diagnostic)
                self.assertEqual([r['path'] for r in json.loads(output)['result']['requests']], expected)


class CLIResponseTests(unittest.TestCase):
    """Boundary replies cover hardware-only outcomes without a serial transport."""

    def invoke(self, arguments, replies):
        from src.lite3_dev_server import cli

        class ScriptedEndpoint:
            def open(endpoint, request, timeout):
                self.assertTrue(replies, 'Unexpected request: ' + request.full_url)
                action, reply = replies.pop(0)
                self.assertEqual(request.full_url.split('/api/')[1], action)
                if callable(reply):
                    reply = reply()
                if isinstance(reply, Exception):
                    raise reply
                return Response(reply)

        output, diagnostic = io.StringIO(), io.StringIO()
        with patch('src.lite3_dev_server.client.build_opener', return_value=ScriptedEndpoint()):
            with redirect_stdout(output), redirect_stderr(diagnostic):
                code = cli.main(arguments)
        self.assertEqual(replies, [], 'CLI skipped expected requests')
        return code, json.loads(output.getvalue()), diagnostic.getvalue()

    def test_acquire_transport_change_releases_without_motion(self):
        acquired = control_status(lease_active=True, simulated=False)
        code, result, diagnostic = self.invoke(
            ['--transport', 'simulated', 'arm', 'jog', 'e', '--delta', '1', '--speed', '1', '--execute'],
            [('status', control_status()), ('control/acquire', {'lease': 'private', 'status': acquired}),
             ('control/release', control_status())])
        self.assertEqual(code, 1)
        self.assertIn('transport', result['error'].lower())

    def test_cancelled_and_faulted_dog_actions_are_failures(self):
        ready = control_status(lease_active=True)
        ready['dog'].update(phase='ready', actions_enabled=True)
        for phase in ('cancelled', 'fault'):
            with self.subTest(phase=phase):
                ended = action_status(phase, lease_active=False)
                ended['dog']['action']['reason'] = 'Fixture ' + phase
                code, result, diagnostic = self.invoke(
                    ['dog', 'forward', '--speed', '.1', '--duration', '1',
                     '--transport', 'simulated', '--execute'],
                    [('status', control_status()),
                     ('control/acquire', {'lease': 'private', 'status': control_status(lease_active=True)}),
                     ('dog/stand', ready), ('dog/action', ended)])
                self.assertEqual(code, 1)
                self.assertIn(phase, result['error'])
                self.assertEqual(result['result']['dog']['action']['phase'], phase)

    def test_hardware_arm_waits_for_operation_and_preserves_unverified_outcome(self):
        for phase, expected_code in (('completed', 0), ('commanded_unverified', 0), ('cancelled', 1), ('fault', 1)):
            with self.subTest(phase=phase):
                status = control_status(simulated=False)
                status['arm'].update(motion_enabled=True, capabilities=arm_capabilities())
                acquired = dict(status, lease_active=True)
                moving = arm_operation()
                accepted = dict(acquired, arm=dict(status['arm'], operation=moving))
                ended = dict(moving, phase=phase)
                code, result, diagnostic = self.invoke(
                    ['arm', 'jog', 'b', '--delta', '.5', '--speed', '1',
                     '--transport', 'real', '--execute'],
                    [('status', status), ('control/acquire', {'lease': 'private', 'status': acquired}),
                     ('arm/jog', accepted), ('control/heartbeat', accepted), ('arm/operation', ended),
                     ('control/release', status)])
                self.assertEqual(code, expected_code, diagnostic)
                self.assertEqual(result['result']['phase'], phase)
                self.assertFalse(result['result']['stop_verified'])
                self.assertIsNone(result['result']['gripper_position_deg'])

    def test_hardware_capability_blocks_jog_before_acquisition(self):
        status = control_status(simulated=False)
        code, result, diagnostic = self.invoke(
            ['arm', 'jog', 'e', '--delta', '1', '--speed', '1', '--transport', 'real', '--execute'],
            [('status', status)])
        self.assertEqual(code, 1)
        self.assertIn('arm', result['error'].lower())

    def test_slow_preparation_heartbeat_cannot_submit_motion_after_wait_deadline(self):
        now = [0.]
        acquired = control_status(lease_active=True)
        preparing = dict(acquired, dog=dict(acquired['dog'], phase='preparing'))
        ready = dict(acquired, dog=dict(acquired['dog'], phase='ready', actions_enabled=True))

        def delayed_ready():
            now[0] += 2.
            return ready

        def sleep(seconds):
            now[0] += seconds

        with patch('src.lite3_dev_server.cli.time.monotonic', side_effect=lambda: now[0]):
            with patch('src.lite3_dev_server.cli.time.sleep', side_effect=sleep):
                code, result, diagnostic = self.invoke(
                    ['dog', 'forward', '--speed', '.1', '--duration', '1', '--transport', 'simulated',
                     '--execute', '--wait-timeout', '.2'],
                    [('status', control_status()), ('control/acquire', {'lease': 'private', 'status': acquired}),
                     ('dog/stand', preparing), ('control/heartbeat', delayed_ready),
                     ('control/release', control_status())])
        self.assertEqual(code, 1)
        self.assertIn('timed out', result['error'])

    def test_slow_arm_heartbeat_cannot_start_an_operation_query_after_deadline(self):
        now = [0.]
        state = control_status(simulated=False, lease_active=True)
        state['arm'].update(motion_enabled=True, capabilities=arm_capabilities(), operation=arm_operation())

        def delayed_heartbeat():
            now[0] += 2.
            return state

        def sleep(seconds):
            now[0] += seconds

        with patch('src.lite3_dev_server.cli.time.monotonic', side_effect=lambda: now[0]):
            with patch('src.lite3_dev_server.cli.time.sleep', side_effect=sleep):
                code, result, diagnostic = self.invoke(
                    ['arm', 'jog', 'b', '--delta', '.5', '--speed', '1', '--transport', 'real',
                     '--execute', '--wait-timeout', '.2'],
                    [('status', state), ('control/acquire', {'lease': 'private', 'status': state}),
                     ('arm/jog', state), ('control/heartbeat', delayed_heartbeat), ('control/release', state)])
        self.assertEqual(code, 1)
        self.assertIn('timed out', result['error'])
        self.assertEqual(result['result']['operation_id'], 'operation-1')

    def test_revoked_arm_lease_still_reports_matching_operation_outcome(self):
        for phase in ('completed', 'cancelled', 'fault'):
            with self.subTest(phase=phase):
                state = control_status(simulated=False, lease_active=True)
                moving = arm_operation()
                state['arm'].update(motion_enabled=True, capabilities=arm_capabilities(), operation=moving)
                conflict = HTTPError('http://127.0.0.1:8080/api/control/heartbeat', 409, 'Conflict',
                                     protocol_headers(), io.BytesIO(b'{"error":"Lease revoked"}'))
                code, result, diagnostic = self.invoke(
                    ['arm', 'jog', 'b', '--delta', '.5', '--speed', '1', '--transport', 'real', '--execute'],
                    [('status', state), ('control/acquire', {'lease': 'private', 'status': state}),
                     ('arm/jog', state), ('control/heartbeat', conflict),
                     ('arm/operation', dict(moving, phase='stopping')),
                     ('arm/operation', dict(moving, phase=phase, hold_status='written'))])
                self.assertEqual(code, 0 if phase == 'completed' else 1, diagnostic)
                self.assertEqual(result['result']['phase'], phase)
                self.assertEqual(result['result']['hold_status'], 'written')
                self.assertEqual(result['result']['operation_id'], 'operation-1')

    def test_revoked_arm_lease_preserves_operation_id_when_observation_is_unavailable(self):
        state = control_status(simulated=False, lease_active=True)
        state['arm'].update(motion_enabled=True, capabilities=arm_capabilities(), operation=arm_operation())
        conflict = HTTPError('http://127.0.0.1:8080/api/control/heartbeat', 409, 'Conflict',
                             protocol_headers(), io.BytesIO(b'{"error":"Lease revoked"}'))
        missing = HTTPError('http://127.0.0.1:8080/api/arm/operation', 404, 'Not found',
                            protocol_headers(), io.BytesIO(b'{"error":"Operation superseded"}'))
        code, result, diagnostic = self.invoke(
            ['arm', 'jog', 'b', '--delta', '.5', '--speed', '1', '--transport', 'real', '--execute'],
            [('status', state), ('control/acquire', {'lease': 'private', 'status': state}),
             ('arm/jog', state), ('control/heartbeat', conflict), ('arm/operation', missing)])
        self.assertEqual(code, 1)
        self.assertEqual(result['http_status'], 409)
        self.assertIn('Lease revoked', result['error'])
        self.assertEqual(result['result']['operation_id'], 'operation-1')


if __name__ == '__main__':
    unittest.main()
