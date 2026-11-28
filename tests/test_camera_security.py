"""Exercise the real HTTP request parser without opening a camera or socket."""
from types import SimpleNamespace
import unittest

from src.camera import LatestFrame
from src.application import ControlApplication
from src.control import ControlService
from src.http_server import CameraRequestHandler
from fixtures.http_connection import MemoryConnection


class CameraSecurityTests(unittest.TestCase):
    frame = b"synthetic-private-frame"

    def request(self, path="/snapshot.jpg", headers=None, source="realsense"):
        # HTTP/parser checks must not depend on scheduling delays.
        frames = LatestFrame(source, stale_after=float("inf"))
        frames.publish(self.frame)
        frames.wait_for_frame = lambda sequence, generation: (None, sequence)
        if headers is None:
            headers = [("Host", "127.0.0.1:18080")]
        raw = "GET {} HTTP/1.1\r\n{}\r\nConnection: close\r\n\r\n".format(
            path, "\r\n".join("{}: {}".format(*header) for header in headers))
        connection = MemoryConnection(raw.encode("latin-1"))
        CameraRequestHandler(connection, ("127.0.0.1", 12345),
                             SimpleNamespace(frames=frames,
                                             application=ControlApplication(ControlService(mode="disabled"))))
        head, body = bytes(connection.output).split(b"\r\n\r\n", 1)
        return int(head.split()[1]), head, body

    def test_camera_data_is_available_without_credentials(self):
        for path in ("/healthz", "/snapshot.jpg", "/stream.mjpg", "/sources"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 200)
                if path in ("/snapshot.jpg", "/stream.mjpg"):
                    self.assertIn(self.frame, body)

    def test_stream_identifies_its_actual_source(self):
        for source in ("realsense", "v4l2"):
            with self.subTest(source=source):
                status, headers, body = self.request("/stream.mjpg", source=source)
                self.assertEqual(status, 200)
                self.assertIn(b"X-Camera-Source: " + source.encode(), headers.split(b"\r\n"))
                self.assertIn(self.frame, body)

    def test_cross_origin_requests_are_rejected(self):
        for origin in ("http://untrusted.example:18080", "http://localhost:18081", "null"):
            with self.subTest(origin=origin):
                headers = [("Host", "localhost:18080"),
                           ("Origin", origin)]
                self.assertEqual(self.request(headers=headers)[0], 403)

    def test_duplicate_origins_are_rejected(self):
        headers = [("Host", "localhost:18080"),
                   ("Origin", "http://localhost:18080"), ("Origin", "http://localhost:18080")]
        self.assertEqual(self.request(headers=headers)[0], 403)

    def test_all_endpoints_work_with_matching_origin(self):
        for host in ("127.0.0.1:18080", "localhost:18081", "camera.example:18082"):
            headers = [("Host", host), ("Origin", "http://" + host)]
            for path in ("/snapshot.jpg", "/stream.mjpg", "/healthz"):
                with self.subTest(host=host, path=path):
                    status, _, body = self.request(path, headers)
                    self.assertEqual(status, 200)
                    if path != "/healthz":
                        self.assertIn(self.frame, body)

    def test_page_identifies_camera_and_keeps_response_headers(self):
        status, headers, body = self.request("/", [("Host", "localhost:18080")])
        self.assertEqual(status, 200)
        self.assertIn(b'class="source">RealSense</span>', body)
        self.assertIn(b'alt="RealSense video stream"', body)
        self.assertNotIn(b"X-Camera-Source:", headers)
        self.assertIn(b"Referrer-Policy: no-referrer", headers)
        self.assertIn(b"Cache-Control: no-store", headers)
        self.assertNotIn(b"Python/", headers)


if __name__ == "__main__":
    unittest.main()
