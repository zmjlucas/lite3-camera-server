"""Offline source discovery, serialized capture switching, and HTTP authorization."""
import errno
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import server


def sources():
    return [
        {"id": "front", "label": "/dev/video4 — D435i RGB", "device": "/dev/video4",
         "backend": "v4l2", "kind": "d435i", "serial": "fixture-usb-not-sdk",
         "usb_path": "/sys/devices/fixture/usb2/2-7", "pixel_format": "YUYV"},
        {"id": "arm", "label": "/dev/video8 — Arm camera (SXW USB Camera)", "device": "/dev/video8",
         "backend": "v4l2", "kind": "sxw", "serial": "",
         "usb_path": "/sys/devices/fixture/usb2/2-9", "pixel_format": "MJPG"},
    ]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sysfs = self.root / "video4linux"
        self.sysfs.mkdir()

    def node(self, number, vendor="8086", product="0b3a", name="Intel RealSense D435I"):
        usb = self.root / ("usb-" + str(number))
        usb.mkdir()
        for key, value in (("idVendor", vendor), ("idProduct", product), ("product", name),
                           ("serial", "fixture-usb")):
            (usb / key).write_text(value)
        node = usb / "interface" / "video4linux" / ("video" + str(number))
        node.mkdir(parents=True)
        try:
            (self.sysfs / node.name).symlink_to(node, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows account lacks the symlink privilege needed for a sysfs fixture")
            raise

    def test_discovery_filters_unsupported_formats_and_hardware_and_sorts_numerically(self):
        self.assertTrue(hasattr(server, "discover_sources"), "Camera discovery is missing")
        self.node(10, "0bda", "525a", "SXW USB Camera")
        self.node(4)
        self.node(5)
        self.node(6)
        self.node(7, "1234", "5678", "Unrelated camera")
        formats = {"/dev/video4": {"YUYV"}, "/dev/video5": set(),
                   "/dev/video6": {"Z16 ", "Y8  "}, "/dev/video7": {"YUYV"},
                   "/dev/video10": {"MJPG", "YUYV"}}
        found = server.discover_sources(self.sysfs, probe=lambda device: formats[device])
        self.assertEqual([entry["device"] for entry in found], ["/dev/video4", "/dev/video10"])
        self.assertEqual([entry["pixel_format"] for entry in found], ["YUYV", "MJPG"])
        self.assertIn("D435i RGB", found[0]["label"])
        self.assertIn("SXW USB Camera", found[1]["label"])
        self.assertNotEqual(found[0]["id"], found[1]["id"])
        self.assertEqual(found[0].get("usb_path"), str((self.root / "usb-4").resolve()))

    def test_product_fallback_and_device_renumbering_do_not_assume_fixed_nodes(self):
        self.assertTrue(hasattr(server, "discover_sources"), "Camera discovery is missing")
        self.node(22, "8086", "ffff", "Intel(R) RealSense(TM) Depth Camera 435i")
        found = server.discover_sources(self.sysfs, probe=lambda device: {"YUYV"})
        self.assertEqual([entry["device"] for entry in found], ["/dev/video22"])

    def test_unreadable_node_does_not_hide_other_cameras(self):
        self.assertTrue(hasattr(server, "discover_sources"), "Camera discovery is missing")
        self.node(4)
        self.node(8, "0bda", "525a", "SXW USB Camera")
        def probe(device):
            if device == "/dev/video4":
                raise PermissionError("not readable")
            return {"MJPG"}
        found = server.discover_sources(self.sysfs, probe=probe)
        self.assertEqual([entry["device"] for entry in found], ["/dev/video8"])

    @unittest.skipIf(os.name == "nt", "fcntl is available on Unix")
    def test_query_uses_node_capabilities_and_enumerates_formats_without_starting_capture(self):
        self.assertTrue(hasattr(server, "v4l2_formats"), "Read-only capability probe is missing")
        import fcntl
        node = self.root / "fake-video"
        node.write_bytes(b"")
        caps = {"node": 0x04000001}
        def ioctl(fd, command, buffer):
            self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
            if command == 0x80685600:
                self.assertEqual(len(buffer), 104)
                struct.pack_into("=III", buffer, 80, 0, 0x84000001, caps["node"])
            elif command == 0xc0405602:
                self.assertEqual(len(buffer), 64)
                index, buffer_type = struct.unpack_from("=II", buffer)
                self.assertEqual(buffer_type, 1)
                if index > 1:
                    raise OSError(errno.EINVAL, "end")
                buffer[44:48] = (b"YUYV", b"MJPG")[index]
            else:
                self.fail("Discovery attempted a capture/configuration ioctl")
            return 0
        with patch.object(fcntl, "ioctl", ioctl):
            self.assertEqual(server.v4l2_formats(str(node)), {"YUYV", "MJPG"})
            caps["node"] = 0x04800000  # Metadata node; parent union still advertises RGB.
            self.assertEqual(server.v4l2_formats(str(node)), set())


class CaptureControllerTests(unittest.TestCase):
    def controller(self, capture_fn=None, sdk=False):
        self.assertTrue(hasattr(server, "CaptureController"), "Capture switching is missing")
        arguments = ["--depth", "--serial", "fixture-front"] if sdk else [
            "--source", "v4l2", "--device", "/dev/video4"]
        self.started = threading.Event()
        self.events = []
        def capture(options, frames, stop):
            self.events.append(("start", options.device, options.source,
                                options.depth, options.serial, getattr(options, "pixel_format", None)))
            frames.publish(options.device.encode() if options.device else b"sdk")
            self.started.set()
            stop.wait()
            self.events.append(("release", options.device))
        controller = server.CaptureController(server.parse_args(arguments), discover=sources,
                                              capture_fn=capture_fn or capture, stop_timeout=0.05)
        self.addCleanup(controller.shutdown)
        controller.start()
        if capture_fn is None:
            self.assertTrue(self.started.wait(1))
        return controller

    def test_switch_releases_previous_camera_and_drops_previous_frame(self):
        controller = self.controller()
        old = controller.frames
        self.assertEqual(controller.catalog()["selected"], "front")
        self.started.clear()
        result = controller.select("arm")
        self.assertTrue(self.started.wait(1))
        self.assertEqual(result["selected"], "arm")
        self.assertEqual([event[:2] for event in self.events], [
            ("start", "/dev/video4"), ("release", "/dev/video4"), ("start", "/dev/video8")])
        self.assertIsNone(old.snapshot()[1])
        old.publish(b"late frame")
        old.fail("late error")
        self.assertIsNone(old.snapshot()[1])
        self.assertEqual(controller.frames.snapshot()[1], b"/dev/video8")
        self.assertEqual(self.events[-1][2:], ("v4l2", False, None, "MJPG"))
        self.assertEqual(set(result["sources"][0]), {"id", "label", "device", "backend"})

    def test_invalid_fresh_selection_leaves_current_capture_running(self):
        controller = self.controller()
        old = controller.frames
        with self.assertRaises(ValueError):
            controller.select("/dev/null")
        controller.discover = lambda: [sources()[0]]
        with self.assertRaises(ValueError):
            controller.select("arm")
        self.assertIs(controller.frames, old)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(old.snapshot()[0]["status"], "live")

    def test_selecting_active_camera_is_a_noop(self):
        controller = self.controller()
        old = controller.frames
        controller.select("front")
        self.assertIs(controller.frames, old)
        self.assertEqual(len(self.events), 1)

    def test_sdk_depth_configuration_returns_after_arm_camera(self):
        controller = self.controller(sdk=True)
        self.assertEqual(controller.catalog()["sources"][0]["backend"], "realsense")
        controller.select("arm")
        self.started.clear()
        controller.select("front")
        self.assertTrue(self.started.wait(1))
        self.assertEqual(self.events[-1][2:5], ("realsense", True, "fixture-front"))
        self.assertIsNone(self.events[-1][1])

    def test_reselecting_failed_camera_restarts_capture_and_can_recover(self):
        started = threading.Event()
        attempts = []
        def capture(options, frames, stop):
            attempts.append(options.device)
            if len(attempts) == 1:
                frames.fail("temporary camera failure")
            else:
                frames.publish(b"recovered")
            started.set()
            stop.wait()
        controller = self.controller(capture)
        self.assertTrue(started.wait(1))
        self.assertEqual(controller.frames.snapshot()[0]["status"], "error")
        started.clear()
        controller.select("front")
        self.assertTrue(started.wait(1), "Selecting a failed active camera did not restart it")
        self.assertEqual(controller.frames.snapshot()[1], b"recovered")

    def test_blocked_stop_refuses_second_capture_and_ignores_late_frames(self):
        blocked = threading.Event()
        started = threading.Event()
        opened = []
        def capture(options, frames, stop):
            opened.append(options.device)
            frames.publish(b"old")
            started.set()
            blocked.wait(2)
            frames.publish(b"late")
        controller = self.controller(capture)
        self.addCleanup(blocked.set)
        self.assertTrue(started.wait(1))
        with self.assertRaises(RuntimeError) as error:
            controller.select("arm")
        self.assertIn("stop", str(error.exception).lower())
        self.assertEqual(opened, ["/dev/video4"])
        self.assertIsNone(controller.frames.snapshot()[1])
        blocked.set()
        controller.producer.join(1)
        self.assertIsNone(controller.frames.snapshot()[1])

    def test_simultaneous_requests_never_capture_two_cameras(self):
        lock = threading.Lock()
        active = [0]
        maximum = [0]
        def capture(options, frames, stop):
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            frames.publish(b"frame")
            stop.wait()
            with lock:
                active[0] -= 1
        controller = self.controller(capture)
        errors = []
        def select(source_id):
            try:
                controller.select(source_id)
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=select, args=(source_id,)) for source_id in ("arm", "front", "arm")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(maximum[0], 1)


class RealSenseIdentityTests(unittest.TestCase):
    usb_path = "/sys/devices/fixture/usb2/2-7"

    def sdk(self, physical_port=None, serial="fixture-sdk", devices=None):
        information = {"physical_port": physical_port or self.usb_path + "/2-7:1.0/video4linux/video0",
                       "serial_number": serial}
        device = SimpleNamespace(get_info=lambda key: information[key])
        enabled = []
        started = []
        stopped = []
        config = SimpleNamespace(enable_device=enabled.append, enable_stream=lambda *args: None)
        def start(config):
            started.append(True)
            return SimpleNamespace(get_device=lambda: device)
        pipeline = SimpleNamespace(start=start, stop=lambda: stopped.append(True))
        sdk = SimpleNamespace(config=lambda: config, pipeline=lambda: pipeline,
                              stream=SimpleNamespace(color=1, depth=2),
                              format=SimpleNamespace(bgr8=1, z16=2),
                              camera_info=SimpleNamespace(physical_port="physical_port", serial_number="serial_number"),
                              context=lambda: SimpleNamespace(query_devices=lambda: devices if devices is not None else [device]))
        return sdk, enabled, started, stopped

    def capture_start(self, options, sdk, frames=None):
        frames = frames or server.LatestFrame("realsense")
        stop = threading.Event()
        stop.set()  # Exercise real startup/identity/cleanup without frame acquisition.
        with patch.dict(sys.modules, {"pyrealsense2": sdk, "numpy": SimpleNamespace(), "cv2": SimpleNamespace()}):
            list(server.realsense_frames(options, frames, stop))
        return frames

    def test_usb_source_is_resolved_to_sdk_serial_across_different_video_interfaces(self):
        options = server.parse_args([])
        options.usb_path = self.usb_path
        options.camera_sources = sources()
        sdk, enabled, started, stopped = self.sdk()
        frames = self.capture_start(options, sdk)
        self.assertEqual(enabled, ["fixture-sdk"])
        self.assertEqual(frames.snapshot()[0].get("source_id"), "front")
        self.assertEqual(started, [True])
        self.assertEqual(stopped, [True])

    def test_unmatched_usb_path_fails_before_opening_a_different_sdk_device(self):
        options = server.parse_args([])
        options.usb_path = self.usb_path
        sdk, enabled, started, _ = self.sdk(physical_port="/sys/devices/fixture/usb2/2-70/2-70:1.0")
        with self.assertRaises(RuntimeError):
            self.capture_start(options, sdk)
        self.assertEqual(enabled, [])
        self.assertEqual(started, [])

    def test_explicit_sdk_serial_cannot_select_a_different_usb_camera(self):
        options = server.parse_args(["--serial", "fixture-other-sdk"])
        options.usb_path = self.usb_path
        sdk, enabled, started, _ = self.sdk()
        with self.assertRaises(RuntimeError):
            self.capture_start(options, sdk)
        self.assertEqual(enabled, [])
        self.assertEqual(started, [])

    def test_explicit_sdk_startup_maps_actual_capture_identity_back_to_catalog(self):
        sdk, enabled, _, _ = self.sdk()
        options = server.parse_args(["--serial", "fixture-sdk"])
        ready = threading.Event()
        failures = []
        def capture(options, frames, stop):
            try:
                self.capture_start(options, sdk, frames)
            except BaseException as error:
                failures.append(error)
            ready.set()
            stop.wait()
        controller = server.CaptureController(options, discover=sources, capture_fn=capture)
        self.addCleanup(controller.shutdown)
        controller.start()
        self.assertTrue(ready.wait(1))
        self.assertEqual(failures, [])
        self.assertEqual(enabled, ["fixture-sdk"])
        self.assertEqual(controller.catalog()["selected"], "front")
        self.assertEqual(controller.frames.source_id, "front")


class MemoryConnection:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()
    def settimeout(self, timeout):
        pass
    def makefile(self, *args, **kwargs):
        return self.input
    def sendall(self, data):
        self.output.extend(data)


class SourceHTTPTests(unittest.TestCase):
    token = "a" * 64

    def setUp(self):
        self.assertTrue(hasattr(server, "CaptureController"), "Capture switching is missing")
        options = server.parse_args(["--source", "v4l2", "--device", "/dev/video4"])
        self.controller = server.CaptureController(options, discover=sources,
                                                   capture_fn=lambda options, frames, stop: stop.wait())
        self.addCleanup(self.controller.shutdown)
        self.controller.start()

    def request(self, method="GET", path="/sources", body=b"", headers=None):
        if headers is None:
            headers = [("Host", "localhost:18080"), ("Authorization", "Bearer " + self.token),
                       ("Origin", "http://localhost:18080")]
        if method == "POST" and not any(key.lower() == "content-length" for key, _ in headers):
            headers += [("Content-Length", str(len(body))), ("Content-Type", "application/json")]
        raw = "{} {} HTTP/1.1\r\n{}\r\nConnection: close\r\n\r\n".format(
            method, path, "\r\n".join("{}: {}".format(*header) for header in headers))
        connection = MemoryConnection(raw.encode() + body)
        http_server = SimpleNamespace(frames=self.controller.frames, controller=self.controller,
                                      session_token=self.token)
        server.CameraRequestHandler(connection, ("127.0.0.1", 12345), http_server)
        head, payload = bytes(connection.output).split(b"\r\n\r\n", 1)
        return int(head.split()[1]), payload

    def test_catalog_and_selection_share_current_session(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["selected"], "front")
        status, body = self.request("POST", "/source", b'{"id":"arm"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["selected"], "arm")
        self.assertEqual(self.request()[0], 200)
        self.assertNotIn(b"fixture-front", body)

    def test_stream_keeps_original_camera_identity_when_another_viewer_switches(self):
        old = self.controller.frames
        old.publish(b"old-camera-jpeg")
        old.wait_for_frame = lambda sequence, generation: (None, sequence)
        connection = MemoryConnection(("GET /stream.mjpg HTTP/1.1\r\nHost: localhost\r\n"
                                       "Authorization: Bearer " + self.token + "\r\n\r\n").encode())
        replacement = server.LatestFrame("v4l2")
        replacement.publish(b"different-camera-jpeg")
        replacements = iter([(b"different-camera-jpeg", 2), (None, 2)])
        replacement.wait_for_frame = lambda sequence, generation: next(replacements, (None, sequence))
        http_server = SimpleNamespace(frames=old, controller=self.controller, session_token=self.token)
        original_send = connection.sendall
        def send(data):
            original_send(data)
            if b"old-camera-jpeg" in data:
                http_server.frames = replacement
        connection.sendall = send
        server.CameraRequestHandler(connection, ("127.0.0.1", 12345), http_server)
        response = bytes(connection.output)
        self.assertIn(b"X-Camera-Source-Id: front\r\n", response)
        self.assertNotIn(b"different-camera-jpeg", response)
        self.assertIn(b"old-camera-jpeg", response)

    def test_new_endpoints_require_authentication_and_same_origin(self):
        for method, path, body in (("GET", "/sources", b""), ("POST", "/source", b'{"id":"arm"}')):
            for headers, expected in (
                    ([("Host", "localhost:18080")], 401),
                    ([("Host", "localhost:18080"), ("Authorization", "Bearer " + self.token),
                      ("Origin", "http://foreign.example")], 403),
                    ([("Host", "localhost:18080"), ("Authorization", "Bearer " + self.token),
                      ("Authorization", "Bearer " + self.token)], 401)):
                with self.subTest(method=method, headers=headers):
                    status, payload = self.request(method, path, body, headers)
                    self.assertEqual(status, expected)
                    self.assertNotIn(b"/dev/video", payload)
            self.assertEqual(self.controller.catalog()["selected"], "front")

    def test_invalid_or_oversized_selection_bodies_do_not_stop_capture(self):
        old = self.controller.frames
        for body in (b"{", b"[]", b"null", b'{"id":4}', b'{"id":"unknown"}',
                     b"[" * 2000 + b"]" * 2000,
                     b'{"id":"arm","device":"/dev/null"}', b"x" * 4097):
            with self.subTest(body=body[:40]):
                status, _ = self.request("POST", "/source", body)
                self.assertIn(status, (400, 413))
                self.assertIs(self.controller.frames, old)
        headers = [("Host", "localhost:18080"), ("Authorization", "Bearer " + self.token),
                   ("Content-Length", "14"), ("Content-Length", "14")]
        self.assertEqual(self.request("POST", "/source", b'{"id":"arm"}', headers)[0], 400)


if __name__ == "__main__":
    unittest.main()
