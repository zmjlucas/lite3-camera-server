"""Exercise launcher transport and service lifecycle without a systemd host."""
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import shlex
import signal
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import launcher


class ServiceLauncherTests(unittest.TestCase):
    def test_service_actions_keep_jump_terminal_and_remote_options(self):
        for action in ('status', 'logs', 'restart'):
            with self.subTest(action=action), patch.object(launcher, 'show_command') as shown, \
                    patch.object(launcher.subprocess, 'call', side_effect=AssertionError('dry-run connected')):
                self.assertEqual(launcher.main(['service', action, 'ysc@robot', '--jump', 'ysc@gateway',
                                                '--remote-port', '8081', '--lines', '12', '--dry-run']), 0)
                command = shown.call_args[0][0]
                self.assertEqual(command[:1], ['ssh'])
                self.assertEqual(command[command.index('-J') + 1], 'ysc@gateway')
                self.assertIn('-tt', command)
                self.assertEqual(command[-2], 'ysc@robot')
                remote = shlex.split(command[-1])
                self.assertIn(action, remote)
                self.assertEqual(remote[remote.index('--port') + 1], '8081')
                self.assertEqual(remote[remote.index('--lines') + 1], '12')

    def test_restart_build_and_timeout_are_quoted_and_exit_code_propagates(self):
        with patch.object(launcher, 'resolve_jump', return_value=None), \
                patch.object(launcher.subprocess, 'call', return_value=7) as run:
            self.assertEqual(launcher.main(['service', 'restart', 'robot', '--expect-build', 'sha256:abc',
                                            '--timeout', '9']), 7)
        remote = shlex.split(run.call_args[0][0][-1])
        self.assertEqual(remote[remote.index('--expect-build') + 1], 'sha256:abc')
        self.assertEqual(remote[remote.index('--timeout') + 1], '9')

    def test_bad_options_fail_before_network_access(self):
        for option in (['--remote-port', '0'], ['--lines', '0'], ['--timeout', '0'],
                       ['--expect-build', 'bad\nheader']):
            with self.subTest(option=option), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(launcher, 'resolve_jump', side_effect=AssertionError('network')):
                with self.assertRaises(SystemExit) as error:
                    launcher.main(['service', 'restart', 'robot'] + option)
                self.assertEqual(error.exception.code, 2)

    def test_build_check_rejects_unsupported_actions_before_connecting(self):
        for action in ('status', 'logs'):
            with self.subTest(action=action), contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(launcher, 'resolve_jump', side_effect=AssertionError('network')):
                with self.assertRaises(SystemExit) as error:
                    launcher.main(['service', action, 'robot', '--expect-build', 'sha256:wrong'])
                self.assertEqual(error.exception.code, 2)


def unit(pid='101', active='active', loaded='loaded', started='100', enabled='enabled'):
    return dict(Id='lite3-dev-server.service', LoadState=loaded, UnitFileState=enabled,
                ActiveState=active, SubState='running' if active == 'active' else 'dead',
                MainPID=pid, ExecMainStartTimestampMonotonic=started,
                ExecMainStatus='0', Result='success', FragmentPath='/etc/systemd/system/lite3-dev-server.service',
                ExecStart='/usr/bin/python3 /home/ysc/lite3-dev-server/server.py --source none',
                DropInPaths='')


def listener(pid='101', address='0.0.0.0'):
    owner = ' users:(("python3",pid={},fd=3))'.format(pid) if pid else ''
    return 'LISTEN 0 5 {}:8080 0.0.0.0:*{}\n'.format(address, owner)


class ServiceManagerTests(unittest.TestCase):
    def setUp(self):
        from src.remote import manage_service
        self.module = manage_service
        self.current = unit()
        self.legacy = unit(pid='0', active='inactive', loaded='not-found')
        self.sockets = listener()
        self.commands = []
        self.http_status = 200
        self.headers = {'X-Lite3-Dev-Protocol': '1', 'X-Lite3-Camera-Protocol': '1',
                        'X-Lite3-Build': 'sha256:new'}
        self.http_calls = []
        self.restart_effect = self.new_process
        self.failure = None
        self.now = 0.0
        self.output = io.StringIO()
        for context in (patch.object(manage_service.subprocess, 'run', side_effect=self.command),
                        patch.object(manage_service, 'HTTPConnection', side_effect=self.connection),
                        patch.object(manage_service, 'probe_timeout', return_value=contextlib.nullcontext()),
                        patch.object(manage_service.time, 'monotonic', side_effect=lambda: self.now),
                        patch.object(manage_service.time, 'sleep', side_effect=self.sleep),
                        contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.output)):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)

    def sleep(self, seconds):
        self.now += seconds

    def new_process(self):
        self.current = unit(pid='202', started='200')
        self.sockets = listener('202')

    def command(self, command, **kwargs):
        self.commands.append(command)
        args = command[2:] if command[:2] == ['sudo', '-n'] else command
        code, output = 0, ''
        if self.failure and self.failure[0] in args:
            code, output = 1, self.failure[1]
        elif args[:2] == ['systemctl', 'show']:
            state = self.legacy if args[-1] == 'lite3-camera-server.service' else self.current
            output = '\n'.join('{}={}'.format(k, v) for k, v in state.items())
        elif 'restart' in args:
            self.restart_effect()
        elif args[0] == 'ss':
            self.assertEqual(args[-1], 'sport = :8080')
            output = self.sockets
        elif args[0] == 'ps':
            output = '303 1 python3 /home/ysc/lite3-dev-server/server.py --source none\n'
        elif args[0] == 'journalctl':
            output = 'journal: startup diagnostic\n'
        elif args != ['sudo', '-v']:
            self.fail('Unexpected external command: {!r}'.format(command))
        return subprocess.CompletedProcess(command, code, stdout=output)

    def connection(self, host, port, timeout):
        self.assertEqual((host, port), ('127.0.0.1', 8080))
        response = SimpleNamespace(status=self.http_status, getheader=self.headers.get,
                                   read=lambda *args: json.dumps({'environment': 'dev', 'mode': 'hardware'}).encode())
        return SimpleNamespace(request=lambda method, path: self.http_calls.append((method, path)),
                               getresponse=lambda: response, close=lambda: None)

    def run_cli(self, action='restart', extra=()):
        with patch.object(self.module.os, 'getuid', return_value=1000, create=True):
            return self.module.main([action, '--timeout', '2'] + list(extra))

    def assert_no_activation(self):
        self.assertFalse(any(word in command for command in self.commands
                             for word in ('restart', 'start', 'stop', 'kill', '--now')))

    def test_status_shows_enabled_state_legacy_unit_and_detached_listener(self):
        self.current = unit(pid='0', active='inactive', enabled='disabled')
        self.legacy = unit(pid='404', active='active')
        self.sockets = listener('303')
        self.assertEqual(self.run_cli('status'), 0)
        for detail in ('disabled', 'inactive', 'lite3-camera-server.service', '303', 'server.py', 'sha256:new'):
            self.assertIn(detail, self.output.getvalue())
        self.assert_no_activation()

    def test_logs_include_both_units_and_requested_line_limit(self):
        self.assertEqual(self.run_cli('logs', ['--lines', '12']), 0)
        command = next(c for c in self.commands if 'journalctl' in c)
        command = command[command.index('journalctl'):]
        self.assertIn('lite3-dev-server.service', command)
        self.assertIn('lite3-camera-server.service', command)
        self.assertEqual(command[command.index('-n') + 1], '12')
        self.assertIn('journal: startup diagnostic', self.output.getvalue())
        self.assert_no_activation()

    def test_restart_verifies_fresh_process_and_camera_independent_http(self):
        self.assertEqual(self.run_cli(extra=['--expect-build', 'sha256:new']), 0)
        self.assertEqual(self.http_calls, [('GET', '/api/status')])
        self.assertIn('202', self.output.getvalue())
        self.assertIn('sha256:new', self.output.getvalue())

    def test_inactive_installed_service_can_be_started_explicitly(self):
        self.current = unit(pid='0', active='inactive', started='0')
        self.sockets = ''
        self.assertEqual(self.run_cli(), 0)

    def test_restart_refuses_active_legacy_even_on_another_port(self):
        self.legacy = unit(pid='404', active='active')
        self.assertEqual(self.run_cli(), 1)
        self.assertIn('lite3-camera-server.service', self.output.getvalue())
        self.assert_no_activation()

    def test_restart_refuses_detached_unknown_and_additional_socket_owners(self):
        for sockets in (listener('303'), listener(''), listener() + listener('303', '[::]')):
            with self.subTest(sockets=sockets):
                self.commands.clear()
                self.sockets = sockets
                self.assertEqual(self.run_cli(), 1)
                self.assertIn('8080', self.output.getvalue())
                self.assert_no_activation()

    def test_missing_or_masked_unit_is_actionable(self):
        for loaded in ('not-found', 'masked'):
            with self.subTest(loaded=loaded):
                self.current['LoadState'] = loaded
                self.assertEqual(self.run_cli(), 1)
                self.assertIn(loaded, self.output.getvalue())
                self.assert_no_activation()

    def test_old_healthy_process_cannot_complete_queued_restart(self):
        self.restart_effect = lambda: None
        self.assertEqual(self.run_cli(), 1)
        self.assertIn('Timed out', self.output.getvalue())

    def test_failed_restart_retains_systemctl_error_and_journal(self):
        self.failure = ('restart', 'Job failed: unit configuration error')
        self.assertEqual(self.run_cli(), 1)
        self.assertIn('unit configuration error', self.output.getvalue())
        self.assertIn('journal: startup diagnostic', self.output.getvalue())

    def test_failed_unit_and_new_port_conflict_are_reported(self):
        for state, sockets in ((unit(pid='0', active='failed'), ''), (unit(pid='202'), listener('303'))):
            with self.subTest(state=state):
                self.current, self.sockets = unit(), listener()
                def fail_start():
                    self.current, self.sockets = state, sockets
                self.restart_effect = fail_start
                self.assertEqual(self.run_cli(), 1)
                self.assertIn('journal: startup diagnostic', self.output.getvalue())

    def test_build_expectation_requires_a_matching_advertised_identifier(self):
        for build in ('sha256:old', None):
            with self.subTest(build=build):
                self.current, self.sockets = unit(), listener()
                self.headers['X-Lite3-Build'] = build
                self.assertEqual(self.run_cli(extra=['--expect-build', 'sha256:new']), 1)
                self.assertIn('build', self.output.getvalue().lower())
                self.assertIn('sha256:new', self.output.getvalue())

    def test_older_server_without_build_works_when_no_expectation_given(self):
        self.headers.pop('X-Lite3-Build')
        self.assertEqual(self.run_cli(), 0)

    def test_redirect_and_wrong_protocol_cannot_pass_readiness(self):
        for status, protocol in ((302, '1'), (200, None), (503, '1')):
            with self.subTest(status=status, protocol=protocol):
                self.current, self.sockets = unit(), listener()
                self.http_status = status
                self.headers['X-Lite3-Dev-Protocol'] = protocol
                self.assertEqual(self.run_cli(), 1)
                self.assertIn('HTTP', self.output.getvalue())

    def test_failed_inspection_prevents_restart_and_preserves_error(self):
        for command in ('show', 'ss'):
            with self.subTest(command=command):
                self.commands.clear()
                self.failure = (command, 'inspection unavailable')
                self.assertEqual(self.run_cli(), 1)
                self.assertIn('inspection unavailable', self.output.getvalue())
                self.assert_no_activation()

    def test_permission_failure_prevents_restart(self):
        original = self.command
        def refuse(command, **kwargs):
            if command == ['sudo', '-v']:
                raise subprocess.CalledProcessError(1, command)
            return original(command, **kwargs)
        with patch.object(self.module.subprocess, 'run', side_effect=refuse):
            self.assertEqual(self.run_cli(), 1)
        self.assert_no_activation()

    def test_missing_legacy_unit_properties_can_have_nonzero_exit(self):
        original = self.command
        def missing(command, **kwargs):
            result = original(command, **kwargs)
            if command[-1] == 'lite3-camera-server.service' and 'show' in command:
                result.returncode = 1
            return result
        with patch.object(self.module.subprocess, 'run', side_effect=missing):
            self.assertEqual(self.run_cli(), 0)

    def test_process_change_during_http_verification_does_not_pass(self):
        original = self.connection
        def changing_connection(*args, **kwargs):
            connection = original(*args, **kwargs)
            def request(method, path):
                self.current = unit(pid=str(int(self.current['MainPID']) + 1), started='300')
                self.sockets = listener(self.current['MainPID'])
            connection.request = request
            return connection
        with patch.object(self.module, 'HTTPConnection', side_effect=changing_connection):
            self.assertEqual(self.run_cli(), 1)
        self.assertIn('process changed', self.output.getvalue())

    def test_preflight_auto_restart_cannot_satisfy_a_queued_restart(self):
        original = self.command
        queued = []
        def auto_restart(command, **kwargs):
            # A healthy replacement appears after the initial baseline sample.
            if 'ss' in command and not queued:
                result = original(command, **kwargs)
                self.new_process()
                return result
            if 'restart' in command:
                queued.append(True)
                if '--no-block' in command:
                    return subprocess.CompletedProcess(command, 0, stdout='')
                # The requested job later fails, after the healthy replacement.
                return subprocess.CompletedProcess(command, 1, stdout='Requested restart job failed')
            return original(command, **kwargs)
        with patch.object(self.module.subprocess, 'run', side_effect=auto_restart):
            self.assertEqual(self.run_cli(), 1)
        self.assertIn('Requested restart job failed', self.output.getvalue())

    def test_restart_wait_uses_full_remaining_deadline(self):
        original = self.command
        def slow_stop(command, **kwargs):
            if 'restart' in command:
                self.assertGreater(kwargs['timeout'], 10)
                self.now += 12
            return original(command, **kwargs)
        with patch.object(self.module.subprocess, 'run', side_effect=slow_stop):
            self.assertEqual(self.run_cli(extra=['--timeout', '20']), 0)

    def test_command_timeout_preserves_partial_stderr(self):
        original = self.command
        def stalled(command, **kwargs):
            if 'restart' in command:
                raise subprocess.TimeoutExpired(command, kwargs['timeout'], output=b'stopping worker stalled\n')
            return original(command, **kwargs)
        with patch.object(self.module.subprocess, 'run', side_effect=stalled):
            self.assertEqual(self.run_cli(), 1)
        self.assertIn('stopping worker stalled', self.output.getvalue())


@unittest.skipUnless(hasattr(signal, 'setitimer'), 'The remote helper runs on Linux')
class ProbeDeadlineTests(unittest.TestCase):
    def test_slow_response_headers_obey_the_total_probe_deadline(self):
        from src.remote.manage_service import ServiceManager
        class SlowHeaders(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    for _ in range(30):
                        self.wfile.write(b'a')
                        self.wfile.flush()
                        time.sleep(0.03)
                    self.wfile.write(b'\r\n\r\n')
                except (ConnectionError, OSError):
                    pass
            def log_message(self, *args):
                pass
        server = HTTPServer(('127.0.0.1', 0), SlowHeaders)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        manager = ServiceManager(port=server.server_port)
        start = time.monotonic()
        manager.deadline = start + 0.2
        with self.assertRaises(RuntimeError):
            manager.http_identity()
        self.assertLess(time.monotonic() - start, 0.7)


if __name__ == '__main__':
    unittest.main()
