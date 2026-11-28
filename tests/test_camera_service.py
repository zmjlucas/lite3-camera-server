"""Offline checks for boot service installation and delayed camera availability."""
import configparser
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from src import camera, server
from src.remote import install_service


@unittest.skipIf(os.name == "nt", "The system service installer runs on the Linux host")
class CameraServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "lite3-dev-server").mkdir()
        (self.home / "lite3-dev-server/server.py").write_text("# deployed server")
        self.calls = []
        self.installed = None

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        actual = command[2:] if command[:2] == ["sudo", "-n"] else command
        if actual[0] == "install":
            self.installed = Path(actual[-2]).read_text()
            self.assertEqual(actual[-1], "/etc/systemd/system/lite3-dev-server.service")
        if actual[:2] == ["systemctl", "list-unit-files"]:
            self.assertEqual(actual[-1], "lite3-camera-server.service")
        return subprocess.CompletedProcess(command, 0, stdout="")

    def test_installs_and_enables_system_service_without_interrupting_viewers(self):
        with patch.object(install_service.subprocess, "run", side_effect=self.run_command), \
                patch.object(install_service.os, "getuid", return_value=1000):
            install_service.install_service(self.home, ["--source", "v4l2", "--device", "/dev/video4", "--port", "8081"])
        unit = configparser.ConfigParser(interpolation=None)
        unit.read_string(self.installed)
        self.assertEqual(unit["Service"]["User"], "1000")
        self.assertIn('"--retry-camera"', unit["Service"]["ExecStart"])
        self.assertIn('"--device" "/dev/video4" "--port" "8081"', unit["Service"]["ExecStart"])
        self.assertEqual(unit["Unit"]["Conflicts"], "lite3-camera-server.service")
        self.assertEqual(self.calls[-2:], [
            ["sudo", "-n", "systemctl", "daemon-reload"],
            ["sudo", "-n", "systemctl", "enable", "lite3-dev-server.service"]])
        self.assertFalse(any(word in command for command in self.calls
                             for word in ("start", "restart", "stop", "--now")))
        self.assertFalse(any("disable" in command for command in self.calls))


class BootCaptureTests(unittest.TestCase):
    def test_capture_retries_after_camera_becomes_available(self):
        # Without retry the first boot-time USB error leaves HTTP permanently unhealthy.
        options = server.parse_args(["--source", "v4l2", "--device", "/dev/video4"])
        options.retry_camera = True
        frames = camera.LatestFrame("v4l2")
        stop = threading.Event()
        attempts = []
        def delayed_camera(options, stop):
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("Camera is still initializing")
            yield b"ready-frame"
            stop.set()
        with patch.object(camera, "v4l2_frames", delayed_camera):
            worker = threading.Thread(target=camera.capture, args=(options, frames, stop), daemon=True)
            worker.start()
            worker.join(7)
            stop.set()
            worker.join(1)
        self.assertEqual(frames.snapshot()[1], b"ready-frame")
        self.assertEqual(len(attempts), 2)
