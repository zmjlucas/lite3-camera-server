"""Real subprocess and HTTP checks for the reusable simulation context."""
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import venv

from src.lite3_dev_server import DevClient
from src.testing import simulated_server


@contextlib.contextmanager
def capture_children(script=None):
    """Record real children; optionally replace the server with a failure fixture."""
    children = []
    popen = subprocess.Popen

    def start(arguments, **kwargs):
        if script is not None:
            arguments = [sys.executable, '-u', '-c', script]
        child = popen(arguments, **kwargs)
        children.append(child)
        return child

    with patch('src.testing.subprocess.Popen', side_effect=start):
        try:
            yield children
        finally:
            # Prevent failed assertions from leaving test children behind.
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


class SimulationTests(unittest.TestCase):
    def test_simulation_ignores_unrelated_installed_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / 'environment'
            venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(str(environment))
            python = environment / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            result = subprocess.run([str(python), '-c',
                                     'import sysconfig; print(sysconfig.get_path("purelib"))'],
                                    check=True, stdout=subprocess.PIPE, text=True)
            unrelated = Path(result.stdout.strip()) / 'remote'
            unrelated.mkdir()
            (unrelated / '__init__.py').write_text(
                "raise RuntimeError('Unrelated installed remote package imported')\n", encoding='utf-8')
            with patch('src.testing.sys.executable', str(python)):
                with simulated_server() as url:
                    self.assertTrue(DevClient(url).status()['dog']['simulated'])

    def test_parallel_contexts_have_ready_http_and_independent_ports(self):
        with capture_children() as children, tempfile.TemporaryDirectory() as directory:
            # Caller working directories and proxy settings must not affect startup.
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {'http_proxy': 'http://127.0.0.1:1',
                                             'HTTP_PROXY': 'http://127.0.0.1:1',
                                             'no_proxy': '', 'NO_PROXY': ''}):
                    with simulated_server() as first, simulated_server() as second:
                        self.assertNotEqual(first, second)
                        for url in (first, second):
                            self.assertTrue(url.startswith('http://127.0.0.1:'))
                            with DevClient(url) as robot:
                                state = robot.status()
                                self.assertEqual(state['environment'], 'dev')
                                self.assertTrue(state['dog']['simulated'])
                                self.assertTrue(state['arm']['simulated'])
                                robot.acquire('context-test')
                                robot.arm_jog('e', 1)
                                self.assertEqual(robot.arm_state()['position_deg']['e'], 1)
                    self.assertEqual(len(children), 2)
                    for child in children:
                        self.assertIsNotNone(child.returncode)
                        if os.name != 'nt':
                            self.assertEqual(child.returncode, 0)
            finally:
                os.chdir(previous)

    def test_body_exception_propagates_after_child_is_reaped(self):
        for error in (LookupError, KeyboardInterrupt):
            with self.subTest(error=error), capture_children() as children:
                with self.assertRaisesRegex(error, 'caller failed'):
                    with simulated_server():
                        raise error('caller failed')
                self.assertIsNotNone(children[0].returncode)

    def test_invalid_timeouts_fail_before_starting_a_child(self):
        for name in ('startup_timeout', 'shutdown_timeout'):
            for value in (0, -1, float('inf'), float('nan')):
                with self.subTest(name=name, value=value), capture_children() as children:
                    with self.assertRaises(ValueError):
                        with simulated_server(**{name: value}):
                            self.fail('Invalid timeout must fail at entry')
                    self.assertEqual(children, [])

    def test_failed_start_includes_stdout_stderr_and_reaps_child(self):
        script = "import sys; print('starting fixture'); print('bind failed', file=sys.stderr); sys.exit(7)"
        with capture_children(script) as children:
            with self.assertRaises(RuntimeError) as failure:
                with simulated_server():
                    self.fail('Startup failure must not yield a URL')
            self.assertIn('starting fixture', str(failure.exception))
            self.assertIn('bind failed', str(failure.exception))
            self.assertIn('7', str(failure.exception))
            self.assertEqual(children[0].returncode, 7)

    def test_startup_timeout_includes_logs_and_reaps_child(self):
        script = "import time; print('still initializing', flush=True); time.sleep(30)"
        with capture_children(script) as children:
            with self.assertRaisesRegex(RuntimeError, 'still initializing'):
                with simulated_server(startup_timeout=0.5, shutdown_timeout=0.1):
                    self.fail('A server without an address must time out')
            self.assertIsNotNone(children[0].returncode)

    def test_reported_url_requires_successful_http_readiness(self):
        requests = []
        ready_after = None
        incomplete_body = False

        class UnreadyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(200 if ready_after and len(requests) >= ready_after else 503)
                self.send_header('X-Lite3-Dev-Protocol', '1')
                self.send_header('X-Lite3-Environment', 'dev')
                self.send_header('X-Lite3-Simulated', 'true')
                if incomplete_body:
                    self.send_header('Content-Length', '100')
                self.end_headers()

            def log_message(self, *args):
                pass

        with HTTPServer(('127.0.0.1', 0), UnreadyHandler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            script = ("import time; print('http://127.0.0.1:{}/', flush=True); time.sleep(30)"
                      .format(server.server_port))
            try:
                for ready_after, incomplete_body in ((None, False), (3, False), (1, True)):
                    requests.clear()
                    with self.subTest(ready_after=ready_after, incomplete_body=incomplete_body), \
                            capture_children(script) as children:
                        if ready_after is None or incomplete_body:
                            with self.assertRaisesRegex(RuntimeError, 'ready'):
                                with simulated_server(startup_timeout=0.5):
                                    self.fail('An unavailable or incomplete HTTP response must not yield a URL')
                        else:
                            with simulated_server() as url:
                                self.assertEqual(url, 'http://127.0.0.1:{}'.format(server.server_port))
                                self.assertGreaterEqual(len(requests), ready_after)
                        self.assertIsNotNone(children[0].returncode)
                    self.assertIn('/api/status', requests)
            finally:
                server.shutdown()
                worker.join(timeout=5)

    @unittest.skipUnless(os.name == 'posix', 'Windows terminate forcibly stops the child')
    def test_shutdown_kills_and_reaps_a_child_that_ignores_termination(self):
        script = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "print('ignoring termination', flush=True); time.sleep(30)")
        with capture_children(script) as children:
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'ignoring termination'):
                with simulated_server(startup_timeout=0.5, shutdown_timeout=0.1):
                    self.fail('The sleeping child must time out')
            self.assertEqual(children[0].returncode, -9)
            self.assertLess(time.monotonic() - started, 5)

    @unittest.skipUnless(os.name == 'posix', 'The fixture ignores POSIX SIGTERM')
    def test_interrupt_during_shutdown_still_kills_and_reaps_child(self):
        script = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                  "print('waiting for shutdown', flush=True); time.sleep(30)")
        popen_type = subprocess.Popen
        wait = popen_type.wait

        def interrupted_wait(child, timeout=None):
            if timeout is not None:
                raise KeyboardInterrupt('interrupted during shutdown')
            return wait(child)

        with capture_children(script) as children:
            with patch.object(popen_type, 'wait', new=interrupted_wait):
                with self.assertRaisesRegex(KeyboardInterrupt, 'interrupted during shutdown'):
                    with simulated_server(startup_timeout=0.5):
                        self.fail('The sleeping child must time out')
            self.assertIsNotNone(children[0].returncode)


if __name__ == '__main__':
    unittest.main()
