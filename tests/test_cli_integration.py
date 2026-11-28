"""Exercise the CLI and real SDK against loopback simulation, including OS signals."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import unittest

from src.camera import LatestFrame
from src.control import ControlService
from src.http_server import CameraHTTPServer, CameraRequestHandler
from src.lite3_dev_server import cli


class CLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.control = ControlService(environment='dev', simulate=True)
        self.addCleanup(self.control.close)
        self.server = CameraHTTPServer(0, LatestFrame('none'), bind='127.0.0.1', control=self.control)
        self.addCleanup(self.server.server_close)
        self.calls = []
        self.after_dispatch = lambda action, result: None
        application = self.server.application
        dispatch = application.dispatch

        def tracked_dispatch(action, body):
            self.calls.append(action)
            result = dispatch(action, body)
            self.after_dispatch(action, result)
            return result

        calls = self.calls

        class TrackingHandler(CameraRequestHandler):
            def _get(handler):
                if handler.path == '/api/status':
                    calls.append('status')
                return super()._get()

        application.dispatch = tracked_dispatch
        self.server.RequestHandlerClass = TrackingHandler
        worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(self.server.shutdown)
        self.control.start()
        self.url = 'http://127.0.0.1:{}'.format(self.server.server_port)

    def invoke(self, arguments=()):
        output, diagnostic = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostic):
            code = cli.main(['--url', self.url] + list(arguments))
        return code, json.loads(output.getvalue()), diagnostic.getvalue()

    def dog_arguments(self, duration=.1):
        return ['--transport', 'simulated', 'dog', 'forward', '--speed', '.1',
                '--duration', str(duration), '--execute']

    def test_default_reads_only_status_and_arm_feedback_requires_explicit_command(self):
        code, result, diagnostic = self.invoke()
        self.assertEqual(code, 0, diagnostic)
        self.assertEqual(self.calls, ['status'])
        self.assertFalse(result['result']['lease_active'])
        self.assertIsNone(self.control._socket)
        code, result, diagnostic = self.invoke(['arm', 'state'])
        self.assertEqual(code, 0, diagnostic)
        self.assertFalse(result['result']['observed'])
        self.assertEqual(self.calls, ['status', 'status', 'arm/state'])

    def test_one_dog_action_completes_and_revokes_its_lease(self):
        code, result, diagnostic = self.invoke(self.dog_arguments())
        self.assertEqual(code, 0, diagnostic)
        state = result['result']
        self.assertEqual(state['dog']['action']['phase'], 'completed')
        self.assertFalse(state['lease_active'])
        self.assertEqual(self.calls.count('control/acquire'), 1)
        self.assertEqual(self.calls.count('dog/stand'), 1)
        self.assertEqual(self.calls.count('dog/action'), 1)
        self.assertIn('control/heartbeat', self.calls)
        self.assertNotIn('dog/stop', self.calls)
        self.assertNotIn('lease', state)
        self.assertIsNone(self.control._socket)

    def start_cli(self, arguments):
        process = subprocess.Popen(
            [sys.executable, '-B', '-m', 'lite3_dev_server', '--url', self.url] + arguments,
            cwd=str(Path(__file__).resolve().parents[1] / 'src'),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        return process

    def wait_for_call(self, process, action):
        deadline = time.monotonic() + 12
        while action not in self.calls and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertIn(action, self.calls, 'CLI exited or timed out before ' + action)

    def assert_interrupted(self, process, signum):
        output, diagnostic = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 128 + signum, diagnostic)
        result = json.loads(output)
        self.assertFalse(result['ok'])
        self.assertEqual(result['kind'], 'interrupted')
        self.assertEqual(result['signal'], signum)
        self.assertNotIn('Traceback', diagnostic)
        self.assertFalse(self.control.status()['lease_active'])
        self.assertIn('control/release', self.calls)
        self.assertNotIn('dog/stop', self.calls)

    @unittest.skipUnless(os.name == 'posix', 'Subprocess POSIX signal delivery')
    def test_signal_during_action_cancels_it_and_repeated_signal_allows_cleanup(self):
        releasing, respond = threading.Event(), threading.Event()
        self.addCleanup(respond.set)

        def hold_release(action, result):
            if action == 'control/release':
                releasing.set()
                respond.wait(3)

        self.after_dispatch = hold_release
        process = self.start_cli(self.dog_arguments(duration=10))
        self.wait_for_call(process, 'dog/action')
        process.send_signal(signal.SIGINT)
        self.assertTrue(releasing.wait(3))
        process.send_signal(signal.SIGTERM)
        respond.set()
        self.assert_interrupted(process, signal.SIGINT)
        self.assertEqual(self.control.status()['dog']['action']['phase'], 'cancelled')
        self.assertEqual(self.calls.count('dog/action'), 1)

    def test_preparation_timeout_releases_lease_without_starting_action(self):
        code, result, diagnostic = self.invoke(self.dog_arguments() + ['--wait-timeout', '.05'])
        self.assertEqual(code, 1)
        self.assertIn('timed out', result['error'].lower())
        self.assertEqual(self.calls[-1], 'control/release')
        self.assertNotIn('dog/action', self.calls)
        self.assertFalse(self.control.status()['lease_active'])


if __name__ == '__main__':
    unittest.main()
