"""Offline source discovery, serialized capture switching, and trusted HTTP access."""
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import camera, camera_sources, server, wide_angle
from src.http_server import CameraRequestHandler
from src.application import ControlApplication
from src.control import ControlService
from fixtures.http_connection import MemoryConnection


MOTION_HOST = "192.168.1.120"


def sources():
    return [
        {"id": "front", "label": "/dev/video4 — D435i RGB", "device": "/dev/video4",
         "backend": "v4l2", "kind": "d435i", "serial": "fixture-usb-not-sdk",
         "usb_path": "/sys/devices/fixture/usb2/2-7", "pixel_format": "YUYV", "available": True, "error": None},
        {"id": "arm", "label": "/dev/video8 — Arm camera (SXW USB Camera)", "device": "/dev/video8",
         "backend": "v4l2", "kind": "sxw", "serial": "",
         "usb_path": "/sys/devices/fixture/usb2/2-9", "pixel_format": "MJPG", "available": True, "error": None},
    ]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sysfs = self.root / "video4linux"
        self.sysfs.mkdir()
        self.proc = self.root / "proc"
        self.proc.mkdir()

    def node(self, number, vendor="8086", product="0b3a", name="Intel RealSense D435I"):
        usb = self.root / ("usb-" + str(number))
        usb.mkdir()
        for key, value in (("idVendor", vendor), ("idProduct", product), ("product", name),
                           ("serial", "fixture-usb")):
            (usb / key).write_text(value)
        node = usb / "interface" / "video4linux" / ("video" + str(number))
        node.mkdir(parents=True)
        (node / "index").write_text("0")
        (node / "name").write_text(name)
        (usb / "interface" / "interface").write_text(name + " RGB")
        try:
            (self.sysfs / node.name).symlink_to(node, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows account lacks the symlink privilege needed for a sysfs fixture")
            raise

    def test_discovery_filters_unsupported_formats_and_hardware_and_sorts_numerically(self):
        self.node(10, "0bda", "525a", "SXW USB Camera")
        self.node(4)
        self.node(5)
        self.node(6)
        self.node(7, "1234", "5678", "Unrelated camera")
        formats = {"/dev/video4": {"YUYV"}, "/dev/video5": set(),
                   "/dev/video6": {"Z16 ", "Y8  "}, "/dev/video7": {"YUYV"},
                   "/dev/video10": {"MJPG", "YUYV"}}
        found = camera_sources.discover_sources(self.sysfs, probe=lambda device: formats[device], proc_root=self.proc)
        self.assertEqual([entry["device"] for entry in found], ["/dev/video4", "/dev/video10"])
        self.assertEqual([entry["pixel_format"] for entry in found], ["YUYV", "MJPG"])
        self.assertIn("D435i RGB", found[0]["label"])
        self.assertIn("SXW USB Camera", found[1]["label"])
        self.assertNotEqual(found[0]["id"], found[1]["id"])
        self.assertEqual(found[0].get("usb_path"), str((self.root / "usb-4").resolve()))


class CaptureControllerTests(unittest.TestCase):
    def controller(self, sdk=False, wide=False):
        arguments = ["--depth", "--serial", "fixture-front"] if sdk else [
            "--source", "v4l2", "--device", "/dev/video4"]
        if wide:
            arguments.extend(["--wide-angle-host", MOTION_HOST])
        self.started = threading.Event()
        self.events = []
        def capture(options, frames, stop):
            self.events.append(("start", options.device, options.source,
                                options.depth, options.serial, getattr(options, "pixel_format", None)))
            frames.publish(options.device.encode() if options.device else b"sdk")
            self.started.set()
            stop.wait()
            self.events.append(("release", options.device))
        controller = camera.CaptureController(server.parse_args(arguments), discover=sources,
                                              capture_fn=capture, stop_timeout=0.05)
        self.addCleanup(controller.shutdown)
        controller.start()
        self.assertTrue(self.started.wait(1))
        return controller

    def test_switch_from_realsense_to_rtsp_to_arm_and_back_releases_every_producer(self):
        controller = self.controller(sdk=True, wide=True)
        with patch.object(wide_angle, "probe") as probe:
            entry = controller.catalog()["sources"][-1]
            self.assertEqual(entry, {"id": entry["id"], "label": "Lite3 wide-angle camera", "device": MOTION_HOST,
                                     "backend": "rtsp", "available": True, "error": None})
            self.assertRegex(entry["id"], r"^camera-[0-9a-f]{24}$")
            front, selected = controller.frames, {}
            for camera_id, backend in ((entry["id"], "rtsp"), ("arm", "v4l2"), ("front", "realsense")):
                with self.subTest(backend=backend):
                    retired = controller.frames
                    self.started.clear()
                    selected = controller.select(camera_id)
                    self.assertTrue(self.started.wait(1))
                    self.assertEqual(selected["selected"], camera_id)
                    self.assertEqual(controller.frames.snapshot()[0]["source"], backend)
                    retired.publish(b"late")
                    self.assertIsNone(retired.snapshot()[1])
            self.assertIsNone(front.snapshot()[1])
            self.assertEqual(probe.call_count, 4)
        self.assertEqual(self.events, [
            ("start", None, "realsense", True, "fixture-front", None),
            ("release", None),
            ("start", None, "rtsp", False, None, None),
            ("release", None),
            ("start", "/dev/video8", "v4l2", False, None, "MJPG"),
            ("release", "/dev/video8"),
            ("start", None, "realsense", True, "fixture-front", None),
        ])

    def test_unavailable_selection_keeps_current_capture_and_refresh_can_recover(self):
        controller = self.controller()
        old = controller.frames
        detected = sources()
        detected[1].update(available=False, error="Camera is busy: realsense-viewer")
        controller.discover = lambda: detected
        catalog = controller.catalog()
        self.assertFalse(catalog["sources"][1]["available"])
        self.assertIn("realsense-viewer", catalog["sources"][1]["error"])
        with self.assertRaisesRegex(RuntimeError, "realsense-viewer"):
            controller.select("arm")
        self.assertIs(controller.frames, old)
        self.assertEqual(old.snapshot()[0]["status"], "live")
        self.assertEqual(len(self.events), 1)
        detected[1].update(available=True, error=None)
        self.assertEqual(controller.select("arm")["selected"], "arm")


class SourceHTTPTests(unittest.TestCase):
    def setUp(self):
        control = ControlService(mode="disabled")
        self.addCleanup(control.close)
        self.application = ControlApplication(control)
        options = server.parse_args(["--source", "v4l2", "--device", "/dev/video4"])
        self.controller = camera.CaptureController(options, discover=sources,
                                                   capture_fn=lambda options, frames, stop: stop.wait())
        self.addCleanup(self.controller.shutdown)
        self.controller.start()

    def test_stream_keeps_original_camera_identity_when_another_viewer_switches(self):
        old = self.controller.frames
        old.publish(b"old-camera-jpeg")
        old.wait_for_frame = lambda sequence, generation: (None, sequence)
        connection = MemoryConnection(("GET /stream.mjpg HTTP/1.1\r\nHost: localhost\r\n"
                                       "\r\n").encode())
        replacement = camera.LatestFrame("v4l2")
        replacement.publish(b"different-camera-jpeg")
        replacements = iter([(b"different-camera-jpeg", 2), (None, 2)])
        replacement.wait_for_frame = lambda sequence, generation: next(replacements, (None, sequence))
        from src.lite3_dev_server._build_info import UNKNOWN_BUILD
        http_server = SimpleNamespace(frames=old, controller=self.controller, application=self.application,
                                      build_info=UNKNOWN_BUILD)
        original_send = connection.sendall
        def send(data):
            original_send(data)
            if b"old-camera-jpeg" in data:
                http_server.frames = replacement
        connection.sendall = send
        CameraRequestHandler(connection, ("127.0.0.1", 12345), http_server)
        response = bytes(connection.output)
        self.assertIn(b"X-Camera-Source-Id: front\r\n", response)
        self.assertNotIn(b"different-camera-jpeg", response)
        self.assertIn(b"old-camera-jpeg", response)


if __name__ == "__main__":
    unittest.main()
