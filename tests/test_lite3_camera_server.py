import http.client
import threading
import unittest
from pathlib import Path

from src.camera import LatestFrame
from src.control import ControlService
from src.http_server import CameraHTTPServer


FIXTURES = Path(__file__).resolve().parent / "fixtures"


class CameraServerTests(unittest.TestCase):
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
        return frame

    def test_multiple_clients_share_frames_and_survive_another_disconnect(self):
        frames = LatestFrame("realsense", stale_after=10)
        control = ControlService(mode="disabled")
        self.addCleanup(control.close)
        server = CameraHTTPServer(0, frames, bind="127.0.0.1", control=control)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join, 4)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        first = (FIXTURES / "camera-frame.jpg").read_bytes()
        # Insert a JPEG comment to distinguish a second valid frame on the wire.
        second = first[:2] + b"\xff\xfe\x00\x06next" + first[2:]
        frames.publish(first)
        one, boundary_one = self.open_stream(server.server_port)
        two, boundary_two = self.open_stream(server.server_port)
        self.assertEqual(self.read_frame(one, boundary_one), first)
        self.assertEqual(self.read_frame(two, boundary_two), first)
        one.close()
        frames.publish(second)
        self.assertEqual(self.read_frame(two, boundary_two), second)
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=4)
        self.addCleanup(connection.close)
        connection.request("GET", "/snapshot.jpg")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), second)
        frames.fail("test complete")
