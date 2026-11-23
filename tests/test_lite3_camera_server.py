import http.client
import json
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from urllib.parse import urlsplit


SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/lite3_camera_server.py"


class CameraServerTests(unittest.TestCase):
    def test_loopback_server_starts_without_reverse_dns(self):
        from scripts.lite3_camera_server import CameraHTTPServer, LatestFrame

        frames = LatestFrame("demo")
        # Loopback HTTP must remain usable when the host's DNS resolver stalls.
        with patch("socket.getfqdn", side_effect=AssertionError("unexpected reverse DNS lookup")):
            with CameraHTTPServer(0, frames) as server:
                self.assertEqual(server.server_address[0], "127.0.0.1")
                self.assertGreater(server.server_port, 0)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    status, _, body = self.request(server.server_port, "/healthz")
                    self.assertEqual(status, 503)
                    self.assertEqual(json.loads(body)["status"], "starting")
                finally:
                    server.shutdown()
                    worker.join(timeout=4)

    def depth_helper(self, body):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        helper = Path(directory.name) / "depth-helper"
        helper.write_text("#!{}\nimport os, signal, struct, sys, time\n".format(sys.executable) + body)
        helper.chmod(0o700)
        return helper

    def wait_health(self, port, expected):
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            status, _, body = self.request(port, "/healthz")
            health = json.loads(body)
            if health["status"] == expected:
                return status, health
            time.sleep(0.025)
        self.fail("health never reached {}: {}".format(expected, health))

    @unittest.skip("Pending feature: native-depth backend is not implemented; see README.md")
    def test_native_depth_requires_helper_and_rejects_rgb_options(self):
        from scripts.lite3_camera_server import parse_args

        for arguments in (
            ["--source", "native-depth"],
            ["--source", "native-depth", "--depth-helper", "/tmp/helper", "--depth"],
            ["--source", "native-depth", "--depth-helper", "/tmp/helper", "--serial", "123"],
            ["--source", "demo", "--depth-helper", "/tmp/helper"],
        ):
            with self.subTest(arguments=arguments), patch("sys.stderr"), self.assertRaises(SystemExit):
                parse_args(arguments)
        options = parse_args(["--source", "native-depth", "--depth-helper", "/tmp/helper"])
        self.assertEqual(options.depth_helper, "/tmp/helper")

    @unittest.skip("Pending feature: native-depth backend is not implemented; see README.md")
    def test_native_depth_serves_helper_jpeg_and_releases_child_on_signals(self):
        from scripts.lite3_camera_server import DEMO_JPEGS
        import base64

        jpeg = base64.b64decode(DEMO_JPEGS[0])
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signum=signum):
                helper = self.depth_helper(
                    "assert sys.argv[1:] == ['--width', '320', '--height', '240', '--fps', '15', '--quality', '80']\n"
                    "def stop(*args):\n"
                    "    open(__file__ + '.released', 'w').write('released')\n"
                    "    sys.exit(0)\n"
                    "signal.signal(signal.SIGTERM, stop)\n"
                    "frame = {!r}\n"
                    "while True:\n"
                    "    os.write(1, struct.pack('<I', len(frame)) + frame)\n"
                    "    time.sleep(0.05)\n".format(jpeg)
                )
                process, port = self.start_server(
                    "native-depth", "--depth-helper", str(helper),
                    "--width", "320", "--height", "240", "--quality", "80",
                )
                self.assertEqual(self.wait_health(port, "live")[0], 200)
                self.assertEqual(self.request(port, "/snapshot.jpg")[2], jpeg)
                self.assertIn(b"RealSense depth", self.request(port, "/")[2])
                process.send_signal(signum)
                _, stderr = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertTrue(Path(str(helper) + ".released").exists(), "helper was not stopped")

    @unittest.skip("Pending feature: native-depth backend is not implemented; see README.md")
    def test_native_depth_rejects_bad_records_and_eof(self):
        cases = (
            ("os.write(1, struct.pack('<I', 9000000))\ntime.sleep(10)\n", "length"),
            ("os.write(1, struct.pack('<I', 4) + b'nope')\ntime.sleep(10)\n", "JPEG"),
            ("os.write(1, struct.pack('<I', 8) + b'xx')\n", "EOF"),
        )
        for body, diagnostic in cases:
            with self.subTest(diagnostic=diagnostic):
                helper = self.depth_helper(body)
                _, port = self.start_server("native-depth", "--depth-helper", str(helper))
                status, health = self.wait_health(port, "error")
                self.assertEqual(status, 503)
                self.assertIn(diagnostic, health["error"])
                self.assertEqual(self.request(port, "/snapshot.jpg")[0], 503)

    @unittest.skip("Pending feature: native-depth backend is not implemented; see README.md")
    def test_native_depth_eof_and_timeout_hide_previously_published_frame(self):
        from scripts.lite3_camera_server import DEMO_JPEGS
        import base64

        jpeg = base64.b64decode(DEMO_JPEGS[0])
        for after_frame, diagnostic in (("time.sleep(0.3)\n", "EOF"), ("time.sleep(10)\n", "timed out")):
            with self.subTest(diagnostic=diagnostic):
                helper = self.depth_helper(
                    "frame = {!r}\nos.write(1, struct.pack('<I', len(frame)) + frame)\n".format(jpeg)
                    + after_frame
                )
                _, port = self.start_server("native-depth", "--depth-helper", str(helper))
                self.assertEqual(self.wait_health(port, "live")[0], 200)
                status, health = self.wait_health(port, "error")
                self.assertEqual(status, 503)
                self.assertIn(diagnostic, health["error"])
                self.assertEqual(self.request(port, "/snapshot.jpg")[0], 503)

    def start_server(self, source, *options):
        process = subprocess.Popen(
            [sys.executable, "-S", str(SERVER_SCRIPT), "--source", source, "--port", "0", *options],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop_process, process)
        with selectors.DefaultSelector() as poller:
            poller.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(poller.select(4), "camera server did not announce a URL")
        url = process.stdout.readline().decode().strip()
        if not url:
            _, stderr = process.communicate(timeout=4)
            self.fail(stderr.decode())
        address = urlsplit(url)
        self.assertEqual(address.hostname, "127.0.0.1")
        return process, address.port

    def start_demo(self):
        process, port = self.start_server("demo")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status, _, body = self.request(port, "/healthz")
            if status == 200:
                self.assertEqual(json.loads(body)["source"], "demo")
                return process, port
            time.sleep(0.025)
        self.fail("demo never published a frame")

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=4)

    @staticmethod
    def request(port, path):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            return response.status, response.headers, response.read()
        finally:
            conn.close()

    def open_stream(self, port):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        self.addCleanup(conn.close)
        conn.request("GET", "/stream.mjpg")
        response = conn.getresponse()
        self.addCleanup(response.close)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "multipart/x-mixed-replace")
        boundary = response.headers.get_param("boundary")
        self.assertTrue(boundary)
        return response, ("--" + boundary + "\r\n").encode()

    def read_frame(self, response, boundary):
        self.assertEqual(response.readline(), boundary)
        headers = http.client.parse_headers(response)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        frame = response.read(int(headers["Content-Length"]))
        self.assertEqual(response.read(2), b"\r\n")
        self.assert_jpeg(frame)
        return frame

    def assert_jpeg(self, frame):
        self.assertTrue(frame.startswith(b"\xff\xd8"))
        self.assertTrue(frame.endswith(b"\xff\xd9"))
        self.assertIn(b"\xff\xda", frame)

    def test_demo_serves_changing_multipart_jpegs(self):
        _, port = self.start_demo()
        response, boundary = self.open_stream(port)
        initial_frame = self.read_frame(response, boundary)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.read_frame(response, boundary) != initial_frame:
                break
        else:
            self.fail("demo stream never changed")

    def test_demo_snapshot_and_page_identify_their_content(self):
        _, port = self.start_demo()
        status, headers, body = self.request(port, "/snapshot.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "image/jpeg")
        self.assert_jpeg(body)
        status, headers, body = self.request(port, "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "text/html")
        self.assertIn(b"Synthetic demo", body)

    def test_signals_shut_down_with_an_active_stream(self):
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signum=signum):
                process, port = self.start_demo()
                response, boundary = self.open_stream(port)
                self.read_frame(response, boundary)
                process.send_signal(signum)
                _, stderr = process.communicate(timeout=4)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertNotIn(b"Traceback", stderr)
                with self.assertRaises(ConnectionRefusedError):
                    self.request(port, "/healthz")

    def test_missing_camera_dependencies_are_visible_in_health_response(self):
        _, port = self.start_server("realsense")
        self.assert_dependency_error(port, "realsense", "pyrealsense2")

    def test_missing_v4l2_dependency_is_visible_in_health_response(self):
        _, port = self.start_server("v4l2", "--device", "/dev/video4")
        self.assert_dependency_error(port, "v4l2", "cv2")
        self.assertIn(b"V4L2 RGB", self.request(port, "/")[2])

    def assert_dependency_error(self, port, source, dependency):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            status, _, body = self.request(port, "/healthz")
            health = json.loads(body)
            if health["status"] == "error":
                break
            time.sleep(0.025)
        else:
            self.fail("{} startup failure never reached health endpoint".format(source))
        self.assertEqual(status, 503)
        self.assertEqual(health["source"], source)
        self.assertIn(dependency, health["error"])
        self.assertIsNone(health["frame_age_s"])

    def test_v4l2_requires_an_explicit_device_and_rejects_realsense_options(self):
        cases = (
            ([], "--device"),
            (["--device", "/dev/null"], "--device"),
            (["--device", "/dev/v4l/by-id/.."], "--device"),
            (["--device", "/dev/video4", "--depth"], "--depth"),
            (["--device", "/dev/video4", "--serial", "123"], "--serial"),
        )
        for arguments, diagnostic in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, "-S", str(SERVER_SCRIPT), "--source", "v4l2", *arguments],
                    capture_output=True, text=True, timeout=4,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(diagnostic, result.stderr.splitlines()[-1])

    def test_v4l2_configures_only_selected_device_and_releases_after_read_failure(self):
        from scripts.lite3_camera_server import LatestFrame, capture, parse_args

        device = "/dev/v4l/by-id/usb-Intel_RealSense-video-index0"
        options = parse_args([
            "--source", "v4l2", "--device", device,
            "--width", "320", "--height", "240", "--fps", "30", "--quality", "80",
        ])
        image = object()
        camera = Mock()
        camera.isOpened.return_value = True
        camera.set.return_value = True
        camera.read.side_effect = [(True, image), (False, None)]
        cv2 = SimpleNamespace(
            VideoCapture=Mock(return_value=camera), CAP_V4L2=200,
            VideoWriter_fourcc=Mock(return_value=1448695129), CAP_PROP_FOURCC=6,
            CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4, CAP_PROP_FPS=5,
            imencode=Mock(return_value=(True, Mock(tobytes=Mock(return_value=b"jpeg")))),
            IMWRITE_JPEG_QUALITY=1,
        )
        frames = LatestFrame("v4l2")
        with patch.dict(sys.modules, {"cv2": cv2}), patch("sys.stderr"):
            capture(options, frames, threading.Event())
        cv2.VideoCapture.assert_called_once_with(device, cv2.CAP_V4L2)
        cv2.VideoWriter_fourcc.assert_called_once_with("Y", "U", "Y", "V")
        self.assertEqual(camera.set.call_args_list, [
            call(6, 1448695129), call(3, 320), call(4, 240), call(5, 30),
        ])
        cv2.imencode.assert_called_once_with(".jpg", image, [1, 80])
        camera.release.assert_called_once()
        health, frame, _ = frames.snapshot()
        self.assertEqual(health["status"], "error")
        self.assertIn(device, health["error"])
        self.assertIn("read", health["error"].lower())
        self.assertIsNotNone(health["frame_age_s"])
        self.assertIsNone(frame)

    def test_v4l2_releases_device_when_open_fails(self):
        from scripts.lite3_camera_server import LatestFrame, capture, parse_args

        options = parse_args(["--source", "v4l2", "--device", "/dev/video4"])
        camera = Mock()
        camera.isOpened.return_value = False
        cv2 = SimpleNamespace(VideoCapture=Mock(return_value=camera), CAP_V4L2=200)
        frames = LatestFrame("v4l2")
        with patch.dict(sys.modules, {"cv2": cv2}), patch("sys.stderr"):
            capture(options, frames, threading.Event())
        cv2.VideoCapture.assert_called_once_with("/dev/video4", cv2.CAP_V4L2)
        camera.release.assert_called_once()
        camera.read.assert_not_called()
        health, frame, _ = frames.snapshot()
        self.assertEqual(health["status"], "error")
        self.assertIn("/dev/video4", health["error"])
        self.assertIn("open", health["error"].lower())
        self.assertIsNone(frame)

    def buffered_server(self, stale_after=2):
        try:
            from scripts.lite3_camera_server import CameraHTTPServer, LatestFrame
        except ModuleNotFoundError as failure:
            self.fail(str(failure))

        frames = LatestFrame("realsense", stale_after=stale_after)
        server = CameraHTTPServer(0, frames)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join, 4)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return frames, server.server_port

    def test_snapshot_serves_only_the_most_recent_frame(self):
        frames, port = self.buffered_server()
        frames.publish(b"earlier")
        frames.publish(b"latest")
        status, _, body = self.request(port, "/snapshot.jpg")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"latest")

    def test_stale_frame_terminates_stream_and_blocks_new_reads(self):
        frames, port = self.buffered_server(stale_after=0.4)
        frames.publish(b"\xff\xd8\xff\xdaframe\xff\xd9")
        response, boundary = self.open_stream(port)
        self.read_frame(response, boundary)
        self.assertEqual(response.read(), boundary[:-2] + b"--\r\n")
        status, _, body = self.request(port, "/healthz")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["status"], "stale")
        for path in ("/snapshot.jpg", "/stream.mjpg"):
            self.assertEqual(self.request(port, path)[0], 503)

    def test_camera_error_disables_previously_published_frame(self):
        frames, port = self.buffered_server()
        self.assertEqual(self.request(port, "/healthz")[0], 503)
        frames.publish(b"previous")
        self.assertEqual(self.request(port, "/snapshot.jpg")[0], 200)
        frames.fail("camera disconnected")
        status, _, body = self.request(port, "/healthz")
        self.assertEqual(status, 503)
        health = json.loads(body)
        self.assertEqual(health["source"], "realsense")
        self.assertEqual(health["status"], "error")
        self.assertEqual(health["error"], "camera disconnected")
        self.assertEqual(self.request(port, "/snapshot.jpg")[0], 503)
        frames.publish(b"recovered")
        self.assertEqual(self.request(port, "/healthz")[0], 200)
        self.assertEqual(self.request(port, "/snapshot.jpg")[2], b"recovered")
