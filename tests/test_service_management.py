"""Exercise guarded service restart without a systemd host."""
import contextlib
import io
import json
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def unit(pid='101', active='active', loaded='loaded', started='100'):
    return dict(Id='lite3-dev-server.service', LoadState=loaded, UnitFileState='enabled',
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
        self.headers = {'X-Lite3-Dev-Protocol': '1', 'X-Lite3-Camera-Protocol': '1',
                        'X-Lite3-Build': 'sha256:new'}
        self.http_calls = []
        self.restart_effect = self.new_process
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
        output = ''
        if args[:2] == ['systemctl', 'show']:
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
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def connection(self, host, port, timeout):
        self.assertEqual((host, port), ('127.0.0.1', 8080))
        response = SimpleNamespace(status=200, getheader=self.headers.get,
                                   read=lambda *args: json.dumps({'environment': 'dev', 'mode': 'hardware'}).encode())
        return SimpleNamespace(request=lambda method, path: self.http_calls.append((method, path)),
                               getresponse=lambda: response, close=lambda: None)

    def run_cli(self, *extra):
        with patch.object(self.module.os, 'getuid', return_value=1000, create=True):
            return self.module.main(['restart', '--timeout', '2', *extra])

    def test_restart_verifies_fresh_process_and_camera_independent_http(self):
        self.assertEqual(self.run_cli('--expect-build', 'sha256:new'), 0)
        self.assertEqual(self.http_calls, [('GET', '/api/status')])
        self.assertIn('202', self.output.getvalue())
        self.assertIn('sha256:new', self.output.getvalue())

    def test_restart_refuses_detached_unknown_and_additional_socket_owners(self):
        for sockets in (listener('303'), listener(''), listener() + listener('303', '[::]')):
            with self.subTest(sockets=sockets):
                self.commands.clear()
                self.sockets = sockets
                self.assertEqual(self.run_cli(), 1)
                self.assertIn('8080', self.output.getvalue())
                self.assertFalse(any(word in command for command in self.commands
                                     for word in ('restart', 'start', 'stop', 'kill', '--now')))

    def test_old_healthy_process_cannot_complete_queued_restart(self):
        self.restart_effect = lambda: None
        self.assertEqual(self.run_cli(), 1)
        self.assertIn('Timed out', self.output.getvalue())

    def test_build_expectation_requires_a_matching_advertised_identifier(self):
        for build in ('sha256:old', None):
            with self.subTest(build=build):
                self.current, self.sockets = unit(), listener()
                self.headers['X-Lite3-Build'] = build
                self.assertEqual(self.run_cli('--expect-build', 'sha256:new'), 1)
                self.assertIn('build', self.output.getvalue().lower())
                self.assertIn('sha256:new', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
