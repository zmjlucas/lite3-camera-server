"""Exercise the real HTTP request parser without opening a camera or socket."""
import io
from types import SimpleNamespace
import unittest

from src.server import CameraRequestHandler, LatestFrame


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


class CameraSecurityTests(unittest.TestCase):
    token = "a" * 64
    frame = b"synthetic-private-frame"

    def request(self, path="/snapshot.jpg", headers=None):
        frames = LatestFrame("realsense", stale_after=0.02)
        frames.publish(self.frame)
        if headers is None:
            headers = [("Host", "127.0.0.1:18080"), ("Authorization", "Bearer " + self.token)]
        raw = "GET {} HTTP/1.1\r\n{}\r\nConnection: close\r\n\r\n".format(
            path, "\r\n".join("{}: {}".format(*header) for header in headers))
        connection = MemoryConnection(raw.encode("latin-1"))
        CameraRequestHandler(connection, ("127.0.0.1", 12345),
                             SimpleNamespace(frames=frames, session_token=self.token))
        head, body = bytes(connection.output).split(b"\r\n\r\n", 1)
        return int(head.split()[1]), head, body

    def test_camera_data_requires_the_current_bearer_token(self):
        for path in ("/healthz", "/snapshot.jpg", "/stream.mjpg"):
            for auth in (None, "Bearer " + "b" * 64, "Basic " + self.token, "Bearer \xff"):
                with self.subTest(path=path, auth=auth):
                    headers = [("Host", "127.0.0.1:18080")]
                    if auth is not None:
                        headers.append(("Authorization", auth))
                    status, _, body = self.request(path, headers)
                    self.assertEqual(status, 401)
                    self.assertNotIn(self.frame, body)
                    self.assertNotIn(self.token.encode(), body)

    def test_query_parameter_cannot_authenticate_camera_requests(self):
        status, _, body = self.request("/snapshot.jpg?token=" + self.token,
                                      [("Host", "127.0.0.1:18080")])
        self.assertEqual(status, 401)
        self.assertNotIn(self.frame, body)

    def test_host_header_does_not_restrict_authenticated_camera_requests(self):
        for hosts in ([], ["untrusted.example:18080"], ["127.0.0.1.evil.example"],
                      ["localhost@evil.example"], ["localhost:0"], ["localhost:65536"],
                      ["localhost:18080", "untrusted.example:18080"]):
            with self.subTest(hosts=hosts):
                headers = [("Host", host) for host in hosts] + [("Authorization", "Bearer " + self.token)]
                status, _, body = self.request(headers=headers)
                self.assertEqual(status, 200)
                self.assertEqual(body, self.frame)

    def test_custom_host_still_requires_session_authentication(self):
        status, _, body = self.request(headers=[("Host", "camera.example:18080")])
        self.assertEqual(status, 401)
        self.assertNotIn(self.frame, body)

    def test_cross_origin_requests_are_rejected_even_with_token(self):
        for origin in ("http://untrusted.example:18080", "http://localhost:18081", "null"):
            with self.subTest(origin=origin):
                headers = [("Host", "localhost:18080"), ("Authorization", "Bearer " + self.token),
                           ("Origin", origin)]
                self.assertEqual(self.request(headers=headers)[0], 403)

    def test_duplicate_authorization_and_origin_are_rejected(self):
        base = [("Host", "localhost:18080"), ("Authorization", "Bearer " + self.token)]
        for extra in ([("Authorization", "Bearer " + self.token)],
                      [("Origin", "http://localhost:18080"), ("Origin", "http://localhost:18080")]):
            with self.subTest(extra=extra):
                self.assertNotEqual(self.request(headers=base + extra)[0], 200)

    def test_valid_token_serves_all_endpoints_with_matching_origin(self):
        for host in ("127.0.0.1:18080", "localhost:18081", "camera.example:18082"):
            headers = [("Host", host), ("Origin", "http://" + host),
                       ("Authorization", "Bearer " + self.token)]
            for path in ("/snapshot.jpg", "/stream.mjpg", "/healthz"):
                with self.subTest(host=host, path=path):
                    status, _, body = self.request(path, headers)
                    self.assertEqual(status, 200)
                    if path != "/healthz":
                        self.assertIn(self.frame, body)

    def test_shell_and_errors_do_not_disclose_session_credentials(self):
        status, headers, body = self.request("/", [("Host", "localhost:18080")])
        self.assertEqual(status, 200)
        self.assertNotIn(self.token.encode(), body)
        self.assertNotIn(b"RealSense", body)
        self.assertIn(b"Referrer-Policy: no-referrer", headers)
        self.assertIn(b"Cache-Control: no-store", headers)
        self.assertNotIn(b"Python/", headers)


if __name__ == "__main__":
    unittest.main()
