"""Offline checks for boot service installation and delayed camera availability."""
import configparser
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import server


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "The system service installer runs on the Linux host")
class CameraServiceTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "src/remote/install_service.py"
        self.assertTrue(path.is_file(), "Boot service installer is missing")
        spec = importlib.util.spec_from_file_location("install_service", path)
        self.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.installer)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "lite3-camera-server").mkdir()
        (self.home / "lite3-camera-server/server.py").write_text("# deployed server")
        self.calls = []
        self.installed = None

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        actual = command[2:] if command[:2] == ["sudo", "-n"] else command
        if actual[0] == "install":
            self.installed = Path(actual[-2]).read_text()
            self.assertEqual(actual[-1], "/etc/systemd/system/lite3-camera-server.service")
        return subprocess.CompletedProcess(command, 0)

    def install(self, uid=1000):
        with patch.object(self.installer.subprocess, "run", side_effect=self.run_command), \
                patch.object(self.installer.os, "getuid", return_value=uid):
            self.installer.install_service(self.home, ["--source", "v4l2", "--device", "/dev/video4",
                                                       "--port", "8081"])

    def test_installs_and_enables_system_service_without_interrupting_viewers(self):
        self.install()
        unit = configparser.ConfigParser(interpolation=None)
        unit.read_string(self.installed)
        self.assertEqual(unit["Service"]["User"], "1000")
        self.assertEqual(unit["Service"]["Type"], "simple")
        self.assertEqual(unit["Service"]["Restart"], "on-failure")
        self.assertEqual(unit["Install"]["WantedBy"], "multi-user.target")
        self.assertIn('"--retry-camera"', unit["Service"]["ExecStart"])
        self.assertIn('"--device" "/dev/video4" "--port" "8081"', unit["Service"]["ExecStart"])
        self.assertEqual(self.calls[-2:], [
            ["sudo", "-n", "systemctl", "daemon-reload"],
            ["sudo", "-n", "systemctl", "enable", "lite3-camera-server.service"]])
        self.assertFalse(any(word in command for command in self.calls
                             for word in ("start", "restart", "stop", "--now")))

    def test_root_installs_without_sudo(self):
        self.install(uid=0)
        self.assertFalse(any(command[0] == "sudo" for command in self.calls))
        self.assertIn("User=0", self.installed)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is only available on Linux")
    def test_generated_unit_is_accepted_by_systemd(self):
        self.home = self.home / 'camera home $cash %percent "quotes"'
        (self.home / "lite3-camera-server").mkdir(parents=True)
        (self.home / "lite3-camera-server/server.py").write_text("# deployed server")
        self.install(uid=os.getuid())
        unit = self.home / "lite3-camera-server.service"
        unit.write_text(self.installed)
        result = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")

    def test_systemd_unavailable_does_not_install_a_unit(self):
        def fail(command, **kwargs):
            raise subprocess.CalledProcessError(1, command)
        self.run_command = fail
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertIsNone(self.installed)

    def test_sudo_refusal_does_not_install_a_unit(self):
        original = self.run_command
        def refuse(command, **kwargs):
            if command[0] == "sudo":
                raise subprocess.CalledProcessError(1, command)
            return original(command, **kwargs)
        self.run_command = refuse
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertIsNone(self.installed)

    def test_install_failure_does_not_enable_or_reload(self):
        original = self.run_command
        def fail(command, **kwargs):
            if "install" in command:
                raise subprocess.CalledProcessError(1, command)
            return original(command, **kwargs)
        self.run_command = fail
        with self.assertRaises(subprocess.CalledProcessError):
            self.install()
        self.assertFalse(any("enable" in command or "daemon-reload" in command for command in self.calls))

    def test_unit_escapes_argument_expansion_and_rejects_control_characters(self):
        value = 'space "quote" \\ slash %h $HOME'
        self.assertEqual(self.installer.unit_quote(value, command=True),
                         '"space \\"quote\\" \\\\ slash %%h $$HOME"')
        self.assertEqual(self.installer.unit_quote(value),
                         '"space \\"quote\\" \\\\ slash %%h $HOME"')
        for value in ("line\nUser=root", "carriage\rreturn", "null\0byte"):
            with self.assertRaises(ValueError):
                self.installer.unit_quote(value)


class BootCaptureTests(unittest.TestCase):
    def test_delayed_sxw_uses_mjpg_and_keeps_the_configured_stable_path(self):
        device = "/dev/v4l/by-id/usb-SXW-video-index0"
        entry = {"id": "sxw", "device": "/dev/video6", "kind": "sxw", "pixel_format": "MJPG",
                 "label": "Arm camera", "backend": "v4l2"}
        options = server.parse_args(["--source", "v4l2", "--device", device, "--retry-camera"])
        opened, formats, released = [], [], []
        stop = threading.Event()
        def open_camera(path, backend):
            opened.append(path)
            def setting(prop, value):
                if prop == 6:
                    formats.append(value)
                    return value == "MJPG"
                return True
            return SimpleNamespace(isOpened=lambda: len(opened) > 1, set=setting,
                                   read=lambda: (True, object()), release=lambda: released.append(True))
        def encode(*args):
            stop.set()
            return True, SimpleNamespace(tobytes=lambda: b"sxw-ready")
        cv2 = SimpleNamespace(VideoCapture=open_camera, CAP_V4L2=200,
                              VideoWriter_fourcc=lambda *chars: "".join(chars), CAP_PROP_FOURCC=6,
                              CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5,
                              imencode=encode, IMWRITE_JPEG_QUALITY=1)
        discover = lambda: [entry] if opened else []
        with patch.dict(sys.modules, {"cv2": cv2}), patch.object(server, "discover_sources", discover), \
                patch.object(server.os.path, "realpath", side_effect=lambda path: "/dev/video6" if path == device else path):
            controller = server.CaptureController(options)
            controller.start()
            # Use the controller's real stop event in the simulated device encoder.
            stop = controller._stop
            controller.producer.join(7)
            health, frame, _ = controller.frames.snapshot()
            controller.shutdown()
        self.assertEqual(frame, b"sxw-ready")
        self.assertEqual(health["source_id"], "sxw")
        self.assertEqual(opened, [device, device])
        self.assertEqual(formats, ["MJPG"])
        self.assertEqual(len(released), 2)

    def test_initial_discovery_does_not_replace_a_configured_by_id_path(self):
        device = "/dev/v4l/by-id/usb-SXW-video-index0"
        entry = {"id": "sxw", "device": "/dev/video6", "kind": "sxw", "pixel_format": "MJPG"}
        options = server.parse_args(["--source", "v4l2", "--device", device, "--retry-camera"])
        paths = []
        def capture(options, frames, stop):
            paths.append(options.device)
        with patch.object(server.os.path, "realpath", side_effect=lambda path: "/dev/video6" if path == device else path):
            controller = server.CaptureController(options, discover=lambda: [entry], capture_fn=capture)
            controller.start()
            controller.producer.join(1)
            controller.shutdown()
        self.assertEqual(paths, [device])

    def test_capture_retries_after_camera_becomes_available(self):
        # Without retry the first boot-time USB error leaves HTTP permanently unhealthy.
        options = server.parse_args(["--source", "v4l2", "--device", "/dev/video4"])
        options.retry_camera = True
        frames = server.LatestFrame("v4l2")
        stop = threading.Event()
        attempts = []
        def delayed_camera(options, stop):
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("Camera is still initializing")
            yield b"ready-frame"
            stop.set()
        with patch.object(server, "v4l2_frames", delayed_camera):
            worker = threading.Thread(target=server.capture, args=(options, frames, stop), daemon=True)
            worker.start()
            worker.join(7)
            stop.set()
            worker.join(1)
        self.assertEqual(frames.snapshot()[1], b"ready-frame")
        self.assertEqual(len(attempts), 2)

    def test_service_retry_wait_can_be_stopped_promptly(self):
        options = server.parse_args(["--source", "v4l2", "--device", "/dev/video4"])
        options.retry_camera = True
        stop = threading.Event()
        failed = threading.Event()
        def missing_camera(options, stop):
            failed.set()
            raise RuntimeError("Camera not ready")
            yield
        with patch.object(server, "v4l2_frames", missing_camera):
            worker = threading.Thread(target=server.capture,
                                      args=(options, server.LatestFrame("v4l2"), stop), daemon=True)
            worker.start()
            self.assertTrue(failed.wait(1))
            stop.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_retry_option_is_available_but_manual_launch_keeps_its_default(self):
        self.assertFalse(getattr(server.parse_args([]), "retry_camera", True))
        self.assertTrue(server.parse_args(["--retry-camera"]).retry_camera)
