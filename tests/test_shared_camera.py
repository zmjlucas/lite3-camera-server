"""Native start/reuse integration; real subprocesses and HTTP, synthetic capture only."""
import http.client
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest

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
            "sys.path.insert(0, {!r})\nfrom src import camera, server\n"
            "def capture(options, frames, stop):\n"
            "    with open({!r}, 'a') as log: log.write('capture\\n')\n"
            "    while not stop.is_set():\n"
            "        frames.publish(b'synthetic-frame')\n"
            "        stop.wait(0.05)\n"
            "camera.capture = capture\n"
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

    def launch(self):
        return subprocess.Popen([sys.executable, str(HELPER), str(self.server), str(self.port),
                                 "--source", "v4l2", "--device", "/dev/video4", "--port", str(self.port),
                                 "--bind", "127.0.0.1", "--control-mode", "disabled"],
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
