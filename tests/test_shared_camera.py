"""Native start/reuse integration; real subprocesses and HTTP, synthetic capture only."""
import http.client
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "src/remote/ensure_camera.py"


@unittest.skipIf(os.name == "nt", "Remote Linux process lifecycle is exercised on POSIX")
class SharedCameraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.root / "server.py"
        self.pid_marker = self.root / "test-server.pid"
        self.server.write_text(
            "import sys, os\nfrom pathlib import Path\n"
            "Path({!r}).write_text(str(os.getpid()))\n"
            "sys.path.insert(0, {!r})\nfrom src import server\n"
            "def capture(options, frames, stop):\n"
            "    with open({!r}, 'a') as log: log.write('capture\\n')\n"
            "    while not stop.is_set():\n"
            "        frames.publish(b'synthetic-frame')\n"
            "        stop.wait(0.05)\n"
            "server.capture = capture\n"
            "sys.exit(server.main())\n".format(str(self.pid_marker), str(ROOT), str(self.root / "captures")))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.addCleanup(self.stop_server)

    def stop_server(self):
        pidfile = self.root / ".runtime" / (str(self.port) + ".pid")
        if not pidfile.exists():
            pidfile = self.pid_marker
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass

    def launch(self, *options, control_mode="disabled"):
        control = ["--control-mode", control_mode] if control_mode is not None else []
        return subprocess.Popen([sys.executable, str(HELPER), str(self.server), str(self.port),
                                 "--source", "v4l2", "--device", "/dev/video4", "--port", str(self.port),
                                 "--bind", "127.0.0.1", *control, *options],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

    def finish(self, process):
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        self.assertEqual(process.returncode, 0, stderr)
        return stdout

    def request(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            connection.request("GET", "/snapshot.jpg")
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_concurrent_launchers_share_one_capture_after_both_exit(self):
        one, two = self.launch(), self.launch()
        output = self.finish(one) + self.finish(two)
        self.assertIn("Started", output)
        self.assertIn("Reusing", output)
        self.assertEqual((self.root / "captures").read_text().splitlines(), ["capture"])
        self.assertEqual(self.request(), (200, b"synthetic-frame"))
        self.assertIn("Reusing", self.finish(self.launch()))
        self.assertEqual((self.root / "captures").read_text().splitlines(), ["capture"])

    def listener(self, status, protocol=None, redirect=None, dev_protocol=None, environment=None, simulated=None,
                 control_mode=None, diagnostics=None):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(status)
                if protocol:
                    self.send_header("X-Lite3-Camera-Protocol", protocol)
                if dev_protocol is not None:
                    self.send_header("X-Lite3-Dev-Protocol", dev_protocol)
                if environment is not None:
                    self.send_header("X-Lite3-Environment", environment)
                if simulated is not None:
                    self.send_header("X-Lite3-Simulated", simulated)
                if control_mode is not None:
                    self.send_header("X-Lite3-Control-Mode", control_mode)
                if diagnostics is not None:
                    self.send_header("X-Lite3-Arm-Diagnostics", diagnostics)
                if redirect:
                    self.send_header("Location", redirect)
                self.end_headers()
                self.wfile.write(b'{"status":"starting"}')
            def log_message(self, *args):
                pass
        listener = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(listener.server_close)
        self.addCleanup(listener.shutdown)
        return listener

    def test_ssh_hangup_during_startup_keeps_a_managed_server(self):
        marker = self.root / "starting"
        source = self.server.read_text().replace(
            "server.capture = capture",
            "import time\ndef discover():\n    Path({!r}).touch()\n"
            "    time.sleep(1.5)\n    return []\n"
            "server.discover_sources = discover\nserver.capture = capture".format(str(marker)))
        self.server.write_text(source)
        process = self.launch()
        deadline = time.monotonic() + 4
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        os.killpg(process.pid, signal.SIGHUP)
        self.finish(process)
        pidfile = self.root / ".runtime" / (str(self.port) + ".pid")
        self.assertTrue(pidfile.exists(), "Hangup left the detached server without its PID file")
        self.assertEqual(self.request(), (200, b"synthetic-frame"))

    def test_later_launcher_waits_for_slow_start_under_the_same_lock(self):
        marker = self.root / "starting"
        source = self.server.read_text().replace(
            "server.capture = capture",
            "import time\ndef discover():\n    Path({!r}).touch()\n"
            "    time.sleep(1.5)\n    return []\n"
            "server.discover_sources = discover\nserver.capture = capture".format(str(marker)))
        self.server.write_text(source)
        one = self.launch()
        deadline = time.monotonic() + 4
        while not marker.exists() and one.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        two = self.launch()
        self.assertIn("Started", self.finish(one))
        self.assertIn("Reusing", self.finish(two))
        self.assertEqual((self.root / "captures").read_text().splitlines(), ["capture"])

    def test_another_account_can_reuse_without_a_deployment_directory(self):
        self.listener(503, "1")
        self.server = self.root / "other-account" / "server.py"
        self.assertIn("Reusing", self.finish(self.launch()))
        self.assertFalse(self.server.parent.exists())
        self.assertFalse((self.root / "captures").exists())

    def test_control_reuses_a_listener_with_matching_environment(self):
        self.listener(503, '1', dev_protocol='1', environment='dist', simulated='false',
                      control_mode='disabled', diagnostics='false')
        self.assertIn('Reusing', self.finish(self.launch('--require-dev', '--env', 'dist')))
        self.assertFalse((self.root / 'captures').exists())

    def test_control_refuses_a_legacy_server_without_environment_identity(self):
        self.listener(503, '1', dev_protocol='1')
        process = self.launch('--require-dev', '--env', 'dist')
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('restart', stderr.lower())
        self.assertFalse((self.root / 'captures').exists())

    def test_camera_view_refuses_a_dev_listener_for_dist(self):
        self.listener(503, '1', dev_protocol='1', environment='dev', simulated='false',
                      control_mode='disabled', diagnostics='false')
        process = self.launch('--env', 'dist')
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('environment', stderr.lower())
        self.assertFalse((self.root / 'captures').exists())

    def test_control_refuses_a_different_simulation_setting(self):
        self.listener(503, '1', dev_protocol='1', environment='dev', simulated='true',
                      control_mode='disabled', diagnostics='false')
        process = self.launch('--require-dev', '--env', 'dev')
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('simulation', stderr.lower())
        self.assertFalse((self.root / 'captures').exists())

    def test_control_refuses_hardware_when_readonly_requested(self):
        self.listener(503, '1', dev_protocol='1', environment='dist', simulated='false',
                      control_mode='hardware', diagnostics='false')
        process = self.launch('--require-dev', control_mode='readonly')
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('control mode', stderr.lower())
        self.assertFalse((self.root / 'captures').exists())

    def test_control_refuses_dog_service_when_arm_diagnostics_requested(self):
        self.listener(503, '1', dev_protocol='1', environment='dev', simulated='false',
                      control_mode='hardware', diagnostics='false')
        process = self.launch('--require-dev', '--env', 'dev', '--arm-motion-profile', '/etc/arm.json',
                              control_mode='hardware')
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('diagnostics', stderr.lower())
        self.assertFalse((self.root / 'captures').exists())

    def test_control_refuses_arm_diagnostics_when_dog_control_requested(self):
        self.listener(503, '1', dev_protocol='1', environment='dev', simulated='false',
                      control_mode='hardware', diagnostics='true')
        process = self.launch('--require-dev', '--env', 'dev', control_mode='hardware')
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('diagnostics', stderr.lower())
        self.assertFalse((self.root / 'captures').exists())

    def test_control_reuses_matching_arm_diagnostics(self):
        self.listener(503, '1', dev_protocol='1', environment='dev', simulated='false',
                      control_mode='hardware', diagnostics='true')
        process = self.launch('--require-dev', '--env', 'dev', '--arm-motion-profile', '/etc/arm.json',
                              control_mode='hardware')
        self.assertIn('Reusing', self.finish(process))
        self.assertFalse((self.root / 'captures').exists())

    def test_control_reuses_default_hardware_with_matching_listener(self):
        self.listener(503, '1', dev_protocol='1', environment='dist', simulated='false',
                      control_mode='hardware', diagnostics='false')
        self.assertIn('Reusing', self.finish(self.launch('--require-dev', control_mode=None)))
        self.assertFalse((self.root / 'captures').exists())

    def test_control_refuses_a_listener_without_control_identity(self):
        self.listener(503, '1', dev_protocol='1', environment='dist', simulated='false')
        process = self.launch('--require-dev')
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('restart', stderr.lower())
        self.assertFalse((self.root / 'captures').exists())

    def test_missing_deployment_explains_how_to_start(self):
        self.server = self.root / "undeployed" / "server.py"
        process = self.launch()
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("Run deploy", stderr)

    def test_redirect_to_another_camera_is_not_reused(self):
        # A valid camera exists, but not on the requested port.
        original = self.port
        self.port = 0
        remote = self.listener(200, "1")
        self.port = original
        self.listener(302, redirect="http://127.0.0.1:{}/healthz".format(remote.server_port))
        process = self.launch()
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("incompatible", stderr)

    def test_foreign_listener_is_not_reused_or_replaced(self):
        self.listener(200)
        process = self.launch()
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("port", stderr.lower())
        self.assertFalse((self.root / "captures").exists())

    def test_failed_start_reports_log_and_does_not_leave_pid(self):
        self.server.write_text("raise SystemExit('synthetic startup failure')\n")
        process = self.launch()
        _, stderr = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("log", stderr.lower())
        self.assertIn("synthetic startup failure", (self.root / ".runtime" / (str(self.port) + ".log")).read_text())
        self.assertFalse((self.root / ".runtime" / (str(self.port) + ".pid")).exists())
