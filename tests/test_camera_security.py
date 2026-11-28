"""Exercise the real HTTP request parser without opening a camera or socket."""
from types import SimpleNamespace
import unittest

from src.camera import LatestFrame
from src.application import ControlApplication
from src.control import ControlService
from src.http_server import CameraRequestHandler
from src.lite3_dev_server._build_info import UNKNOWN_BUILD
from fixtures.http_connection import MemoryConnection


class CameraSecurityTests(unittest.TestCase):
    def test_cross_origin_requests_are_rejected(self):
        frames = LatestFrame("realsense", stale_after=float("inf"))
        frames.publish(b"synthetic-private-frame")
        control = ControlService(mode="disabled")
        self.addCleanup(control.close)
        server = SimpleNamespace(frames=frames, build_info=UNKNOWN_BUILD, application=ControlApplication(control))
        for origin in ("http://untrusted.example:18080", "http://localhost:18081", "null"):
            with self.subTest(origin=origin):
                raw = "GET /snapshot.jpg HTTP/1.1\r\nHost: localhost:18080\r\nOrigin: {}\r\n\r\n".format(origin)
                connection = MemoryConnection(raw.encode("ascii"))
                CameraRequestHandler(connection, ("127.0.0.1", 12345), server)
                self.assertEqual(int(bytes(connection.output).split()[1]), 403)


if __name__ == "__main__":
    unittest.main()
