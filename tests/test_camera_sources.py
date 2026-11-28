"""Offline source discovery, serialized capture switching, and trusted HTTP access."""
import errno
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
        self.descriptor_targets = {}
        readlink = os.readlink
        def proc_readlink(path, *args, **kwargs):
            if str(path) in self.descriptor_targets:
                return self.descriptor_targets[str(path)]
            return readlink(path, *args, **kwargs)
        patcher = patch.object(os, "readlink", proc_readlink)
        patcher.start()
        self.addCleanup(patcher.stop)

    def discover(self, probe):
        return camera_sources.discover_sources(self.sysfs, probe=probe, proc_root=self.proc)

    def owner(self, pid, device, name=b"realsense-viewer\n"):
        process = self.proc / str(pid)
        (process / "fd").mkdir(parents=True, exist_ok=True)
        (process / "comm").write_bytes(name)
        descriptor = process / "fd" / str(len(self.descriptor_targets))
        descriptor.touch()
        # Emulate Linux proc link values even on Windows, where readlink normalizes paths.
        self.descriptor_targets[str(descriptor)] = device
        return descriptor

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
        found = self.discover(probe=lambda device: formats[device])
        self.assertEqual([entry["device"] for entry in found], ["/dev/video4", "/dev/video10"])
        self.assertEqual([entry["pixel_format"] for entry in found], ["YUYV", "MJPG"])
        self.assertIn("D435i RGB", found[0]["label"])
        self.assertIn("SXW USB Camera", found[1]["label"])
        self.assertNotEqual(found[0]["id"], found[1]["id"])
        self.assertEqual(found[0].get("usb_path"), str((self.root / "usb-4").resolve()))

    def test_product_fallback_and_device_renumbering_do_not_assume_fixed_nodes(self):
        self.node(22, "8086", "ffff", "Intel(R) RealSense(TM) Depth Camera 435i")
        found = self.discover(probe=lambda device: {"YUYV"})
        self.assertEqual([entry["device"] for entry in found], ["/dev/video22"])

    def test_unreadable_rgb_node_remains_visible_with_error_and_recovers(self):
        self.node(4)
        self.node(8, "0bda", "525a", "SXW USB Camera")
        def probe(device):
            if device == "/dev/video4":
                raise PermissionError("not readable")
            return {"MJPG"}
        found = self.discover(probe=probe)
        self.assertEqual([entry["device"] for entry in found], ["/dev/video4", "/dev/video8"])
        self.assertFalse(found[0]["available"])
        self.assertIn("not readable", found[0]["error"])
        self.assertTrue(found[1]["available"])
        recovered = self.discover(probe=lambda device: {"YUYV", "MJPG"})
        self.assertEqual(recovered[0]["id"], found[0]["id"])
        self.assertTrue(recovered[0]["available"])
        self.assertIsNone(recovered[0]["error"])

    def test_failed_probe_does_not_offer_metadata_or_depth_nodes(self):
        for number in (4, 5, 6, 8, 9):
            self.node(number, *(('0bda', '525a', 'SXW USB Camera') if number >= 8 else ()))
        for number in (5, 9):
            (self.sysfs / ("video" + str(number)) / "index").write_text("1")
        (self.root / "usb-6" / "interface" / "interface").write_text("RealSense Depth")
        def probe(device):
            raise OSError(errno.EBUSY, "Device or resource busy")
        found = self.discover(probe=probe)
        self.assertEqual([entry["device"] for entry in found], ["/dev/video4", "/dev/video8"])
        self.assertTrue(all(not entry["available"] and "busy" in entry["error"] for entry in found))

    def test_external_owner_disables_camera_even_when_formats_succeed(self):
        self.node(4)
        self.node(8, "0bda", "525a", "SXW USB Camera")
        self.owner(os.getpid(), "/dev/video8")
        external = self.owner(os.getpid() + 1000, "/dev/video4")
        # A process which vanished during enumeration must not hide cameras.
        (self.proc / "999999").mkdir()
        found = self.discover(probe=lambda device: {"YUYV", "MJPG"})
        self.assertFalse(found[0]["available"])
        self.assertIn("realsense-viewer", found[0]["error"])
        self.assertTrue(found[1]["available"], "The server's own capture was treated as busy")
        external.unlink()
        recovered = self.discover(probe=lambda device: {"YUYV", "MJPG"})
        self.assertTrue(all(entry["available"] for entry in recovered))

    def test_owner_name_with_invalid_bytes_does_not_abort_discovery(self):
        self.node(4)
        self.owner(os.getpid() + 1000, "/dev/video4", name=b"viewer-\xff\n")
        found = self.discover(probe=lambda device: {"YUYV"})
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0]["available"])
        self.assertIn("viewer-", found[0]["error"])
        self.assertIn("PID", found[0]["error"])

    def test_usb_and_sibling_handles_are_matched_to_the_same_camera(self):
        self.node(4)
        usb = self.root / "usb-4"
        (usb / "busnum").write_text("2")
        (usb / "devnum").write_text("7")
        sibling = usb / "depth" / "video4linux" / "video0"
        sibling.mkdir(parents=True)
        (self.sysfs / "video0").symlink_to(sibling, target_is_directory=True)
        for target in ("/dev/video0", "/dev/bus/usb/002/007"):
            with self.subTest(target=target):
                handle = self.owner(os.getpid() + 1000, target)
                found = self.discover(probe=lambda device: {"YUYV"} if device == "/dev/video4" else {"Z16 "})
                self.assertEqual(len(found), 1)
                self.assertEqual(set(found[0]['related_devices']),
                                 {'/dev/video4', '/dev/video0', '/dev/bus/usb/002/007'})
                self.assertFalse(found[0]["available"])
                self.assertIn(target, found[0]["error"])
                handle.unlink()

    @unittest.skipIf(os.name == "nt", "fcntl is available on Unix")
    def test_query_uses_node_capabilities_and_enumerates_formats_without_starting_capture(self):
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
            self.assertEqual(camera_sources.v4l2_formats(str(node)), {"YUYV", "MJPG"})
            with patch.object(os, "access", return_value=False), self.assertRaises(PermissionError):
                camera_sources.v4l2_formats(str(node))
            caps["node"] = 0x04800000  # Metadata node; parent union still advertises RGB.
            self.assertEqual(camera_sources.v4l2_formats(str(node)), set())


class CaptureControllerTests(unittest.TestCase):
    def controller(self, capture_fn=None, sdk=False, wide=False):
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
                                              capture_fn=capture_fn or capture, stop_timeout=0.05)
        self.addCleanup(controller.shutdown)
        controller.start()
        if capture_fn is None:
            self.assertTrue(self.started.wait(1))
        return controller

    def test_local_startup_never_probes_rtsp_and_default_catalog_does_not_probe(self):
        with patch.object(wide_angle, "probe", side_effect=AssertionError("unexpected RTSP probe")):
            configured = self.controller(wide=True)
            self.assertEqual(configured.frames.source, "v4l2")
            default = self.controller()
            self.assertEqual(default.catalog()["selected"], "front")
            self.assertEqual(default.select("arm")["selected"], "arm")

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
        self.assertEqual(set(result["sources"][0]), {"id", "label", "device", "backend", "available", "error"})

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

    def test_selecting_active_camera_is_a_noop(self):
        controller = self.controller()
        old = controller.frames
        controller.select("front")
        self.assertIs(controller.frames, old)
        self.assertEqual(len(self.events), 1)

    def test_active_camera_newly_unavailable_returns_error_without_restarting(self):
        controller = self.controller()
        old = controller.frames
        detected = sources()
        detected[0].update(available=False, error="Camera is busy")
        controller.discover = lambda: detected
        with self.assertRaisesRegex(RuntimeError, "busy"):
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
        frames = frames or camera.LatestFrame("realsense")
        stop = threading.Event()
        stop.set()  # Exercise real startup/identity/cleanup without frame acquisition.
        with patch.dict(sys.modules, {"pyrealsense2": sdk, "numpy": SimpleNamespace(), "cv2": SimpleNamespace()}):
            list(camera.realsense_frames(options, frames, stop))
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
        controller = camera.CaptureController(options, discover=sources, capture_fn=capture)
        self.addCleanup(controller.shutdown)
        controller.start()
        self.assertTrue(ready.wait(1))
        self.assertEqual(failures, [])
        self.assertEqual(enabled, ["fixture-sdk"])
        self.assertEqual(controller.catalog()["selected"], "front")
        self.assertEqual(controller.frames.source_id, "front")


class SourceHTTPTests(unittest.TestCase):

    def setUp(self):
        self.application = ControlApplication(ControlService(mode="disabled"))
        self.start_controller()

    def start_controller(self, *extra):
        options = server.parse_args(["--source", "v4l2", "--device", "/dev/video4", *extra])
        self.controller = camera.CaptureController(options, discover=sources,
                                                   capture_fn=lambda options, frames, stop: stop.wait())
        self.addCleanup(self.controller.shutdown)
        self.controller.start()

    def request(self, method="GET", path="/sources", body=b"", headers=None):
        if headers is None:
            headers = [("Host", "localhost:18080"),
                       ("Origin", "http://localhost:18080")]
        if method == "POST" and not any(key.lower() == "content-length" for key, _ in headers):
            headers += [("Content-Length", str(len(body))), ("Content-Type", "application/json")]
        raw = "{} {} HTTP/1.1\r\n{}\r\nConnection: close\r\n\r\n".format(
            method, path, "\r\n".join("{}: {}".format(*header) for header in headers))
        connection = MemoryConnection(raw.encode() + body)
        from src.lite3_dev_server._build_info import UNKNOWN_BUILD
        http_server = SimpleNamespace(frames=self.controller.frames, controller=self.controller,
                                      build_info=UNKNOWN_BUILD,
                                      application=self.application)
        CameraRequestHandler(connection, ("127.0.0.1", 12345), http_server)
        head, payload = bytes(connection.output).split(b"\r\n\r\n", 1)
        self.response_headers = head
        return int(head.split()[1]), payload

    def test_catalog_and_selection_work_without_credentials(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["selected"], "front")
        status, body = self.request("POST", "/source", b'{"id":"arm"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["selected"], "arm")
        self.assertEqual(self.request()[0], 200)
        self.assertNotIn(b"fixture-front", body)

    def test_disabled_rtsp_source_preserves_live_local_generation_and_recovers_with_same_id(self):
        self.controller.shutdown()
        self.start_controller("--wide-angle-host", MOTION_HOST)
        local = self.controller.frames
        local.publish(b"local-frame")
        with patch.object(wide_angle, "probe", side_effect=RuntimeError(
                "Lite3 wide-angle camera: RTSP DESCRIBE returned 404. Check then Refresh.")) as probe:
            status, body = self.request()
            self.assertEqual(status, 200)
            self.assertNotIn(b"rtsp://", body)
            catalog = json.loads(body)
            self.assertEqual(catalog["selected"], "front")
            front, arm, wide = catalog["sources"]
            self.assertTrue(front["available"])
            self.assertTrue(arm["available"])
            self.assertFalse(wide["available"])
            self.assertIn("404", wide["error"])
            selection = json.dumps({"id": wide["id"]}).encode()
            status, body = self.request("POST", "/source", selection)
            self.assertEqual(status, 409)
            self.assertIn(b"Refresh", body)
            self.assertIs(self.controller.frames, local)
            self.assertEqual(self.request(path="/snapshot.jpg"), (200, b"local-frame"))
            self.assertEqual(probe.call_count, 2)

        with patch.object(wide_angle, "probe") as probe:
            for body in (b'{"id":"rtsp://192.168.1.120:8554/test"}',
                         b'{"id":"wide","host":"127.0.0.1"}'):
                self.assertEqual(self.request("POST", "/source", body)[0], 400)
            self.assertIs(self.controller.frames, local)
            recovered = json.loads(self.request()[1])["sources"][-1]
            self.assertEqual(recovered["id"], wide["id"])
            self.assertTrue(recovered["available"])
            self.assertIsNone(recovered["error"])
            self.assertEqual(self.request("POST", "/source", selection)[0], 200)
            self.assertEqual(probe.call_count, 3)

        frames = self.controller.frames
        frames.publish(b"wide-frame")
        status, body = self.request(path="/healthz")
        health = json.loads(body)
        self.assertEqual((status, health["source"], health["source_id"]), (200, "rtsp", wide["id"]))
        frames.wait_for_frame = lambda seq, generation: (None, seq)
        status, body = self.request(path="/stream.mjpg")
        self.assertEqual(status, 200)
        self.assertIn(b"wide-frame", body)
        self.assertIn(b"X-Camera-Source: rtsp", self.response_headers)
        self.assertIn("X-Camera-Source-Id: {}".format(wide["id"]).encode(), self.response_headers)
        self.assertIn(b"Lite3 wide-angle camera", self.request(path="/")[1])

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

    def test_source_endpoints_still_require_same_origin(self):
        for method, path, body in (("GET", "/sources", b""), ("POST", "/source", b'{"id":"arm"}')):
            status, _ = self.request(method, path, body,
                                     [("Host", "localhost:18080"), ("Origin", "http://foreign.example")])
            self.assertEqual(status, 403)
        self.assertEqual(self.controller.catalog()["selected"], "front")

    def test_busy_selection_returns_conflict_with_reason_without_stopping_capture(self):
        old = self.controller.frames
        detected = sources()
        detected[1].update(available=False, error="Permission denied: /dev/video8")
        self.controller.discover = lambda: detected
        status, body = self.request("POST", "/source", b'{"id":"arm"}')
        self.assertEqual(status, 409)
        self.assertIn("Permission denied", json.loads(body)["error"])
        self.assertIs(self.controller.frames, old)

    def test_invalid_or_oversized_selection_bodies_do_not_stop_capture(self):
        old = self.controller.frames
        for body in (b"{", b"[]", b"null", b'{"id":4}', b'{"id":"unknown"}',
                     b"[" * 2000 + b"]" * 2000,
                     b'{"id":"arm","device":"/dev/null"}', b"x" * 4097):
            with self.subTest(body=body[:40]):
                status, _ = self.request("POST", "/source", body)
                self.assertIn(status, (400, 413))
                self.assertIs(self.controller.frames, old)
        headers = [("Host", "localhost:18080"),
                   ("Content-Length", "14"), ("Content-Length", "14")]
        self.assertEqual(self.request("POST", "/source", b'{"id":"arm"}', headers)[0], 400)


if __name__ == "__main__":
    unittest.main()
