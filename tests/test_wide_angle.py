"""Exercise the RTSP probe with a fragmented response."""
from collections import deque
import unittest
from unittest.mock import patch

from src import wide_angle


HOST = "192.168.1.120"
REQUEST = (
    b"DESCRIBE rtsp://192.168.1.120:8554/test RTSP/1.0\r\n"
    b"CSeq: 1\r\nAccept: application/sdp\r\n\r\n"
)
SDP = b"v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"


class ProbeSocket:
    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.address = None
        self.request = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True

    def settimeout(self, timeout):
        pass

    def connect(self, address):
        self.address = address

    def sendall(self, request):
        self.request = request

    def recv(self, maximum):
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if len(chunk) > maximum:
            self.chunks.appendleft(chunk[maximum:])
        return chunk[:maximum]


class ProbeTests(unittest.TestCase):
    def test_describe_uses_fixed_endpoint_and_reads_fragmented_sdp(self):
        payload = (b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: "
                   + str(len(SDP)).encode("ascii") + b"\r\n\r\n" + SDP)
        sock = ProbeSocket([payload[:12], payload[12:40], payload[40:46], payload[46:-3], payload[-3:]])
        with patch.object(wide_angle.socket, "socket", return_value=sock):
            wide_angle.probe(HOST)
        self.assertTrue(sock.closed)
        self.assertEqual(sock.request, REQUEST)
        self.assertEqual(sock.address, (HOST, 8554))


if __name__ == "__main__":
    unittest.main()
