"""Offline RTSP protocol and supervised decoder tests."""
from collections import deque
import contextlib
import io
import os
from pathlib import Path
from queue import Queue
import shlex
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src import client, server, wide_angle


HOST = "192.168.1.120"
REQUEST = (
    b"DESCRIBE rtsp://192.168.1.120:8554/test RTSP/1.0\r\n"
    b"CSeq: 1\r\nAccept: application/sdp\r\n\r\n"
)
SDP = b"v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"


def response(body=SDP):
    return (b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: "
            + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)


class ProbeSocket:
    def __init__(self, chunks, connect_error=None):
        self.chunks = deque(chunks)
        self.connect_error = connect_error
        self.address = None
        self.request = None
        self.closed = False
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.closed = True

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.address = address
        if self.connect_error:
            raise self.connect_error

    def sendall(self, request):
        self.request = request

    def recv(self, maximum):
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if isinstance(chunk, Exception):
            raise chunk
        if len(chunk) > maximum:
            self.chunks.appendleft(chunk[maximum:])
        return chunk[:maximum]


class ProbeTests(unittest.TestCase):
    def probe(self, sock):
        with patch.object(wide_angle.socket, "socket", return_value=sock):
            try:
                return wide_angle.probe(HOST)
            finally:
                self.assertTrue(sock.closed)

    def test_rejects_noncanonical_or_nonunicast_host(self):
        self.assertEqual(wide_angle.validate_host(HOST), HOST)
        self.assertEqual(wide_angle.validate_host("127.0.0.1"), "127.0.0.1")
        for host in ("192.168.001.1", "0.0.0.0", "224.0.0.1", "255.255.255.255",
                     "camera.local", "::1", "192.168.1.120:8554", "rtsp://192.168.1.120/test",
                     "192.168.1.120\r\n", " 192.168.1.120", ""):
            with self.subTest(host=host), self.assertRaises(ValueError):
                wide_angle.validate_host(host)

    def test_describe_uses_fixed_endpoint_and_reads_fragmented_sdp(self):
        payload = response()
        sock = ProbeSocket([payload[:12], payload[12:40], payload[40:46], payload[46:-3], payload[-3:]])
        self.probe(sock)
        self.assertEqual(sock.request, REQUEST)
        self.assertEqual(sock.address, (HOST, 8554))

    def test_describe_accepts_numeric_cseq_and_length_with_leading_zeroes(self):
        payload = response().replace(b"CSeq: 1", b"CSeq: 0001").replace(
            b"Content-Length: ", b"Content-Length: 00000")
        self.probe(ProbeSocket([payload]))

    def test_live_port_without_publisher_or_redirect(self):
        for status in (404, 302):
            with self.subTest(status=status), self.assertRaisesRegex(
                    RuntimeError, "wide-angle.*{}.*Refresh".format(status)):
                self.probe(ProbeSocket([b"RTSP/1.0 %d Publisher unavailable\r\nCSeq: 1\r\n\r\n" % status]))

    def test_connect_failure_is_camera_specific(self):
        with self.assertRaisesRegex(RuntimeError, "wide-angle.*refused.*Refresh"):
            self.probe(ProbeSocket([], ConnectionRefusedError("connection refused")))

    def test_header_and_body_timeouts(self):
        for chunks in ([b"RTSP/1.0 ", socket.timeout()], [response()[:-1], socket.timeout()]):
            with self.subTest(chunks=chunks), self.assertRaisesRegex(
                    RuntimeError, "wide-angle.*timed out.*Refresh"):
                self.probe(ProbeSocket(chunks))

    def test_partial_replies_share_a_monotonic_deadline(self):
        sock = ProbeSocket([b"RTSP/1.0 ", b"200 OK\r\n", response()[17:]])
        with patch.object(wide_angle.time, "monotonic", side_effect=[10, 10.15, 10.3, 10.5, 10.7, 10.75]):
            with self.assertRaisesRegex(RuntimeError, "wide-angle.*timed out.*Refresh"):
                self.probe(sock)
        self.assertLess(sock.timeout, 0.1)

    def test_malformed_or_oversized_replies_are_rejected(self):
        headers, body = response().split(b"\r\n\r\n", 1)
        for payload, reason in (
            (headers, "headers"),
            (b"X" * 8193, "headers"),
            (headers.replace(b"CSeq: 1", b"CSeq: 2") + b"\r\n\r\n" + body, "CSeq"),
            (headers.replace(b"Content-Length: ", b"Content-Length: +") + b"\r\n\r\n", "length"),
            (b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 8193\r\n\r\n", "length"),
            (headers + b"\r\n\r\n" + body[:-1], "body"),
            (response(b"v=0\r\nm=audio 0 RTP/AVP 96\r\n"), "video"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(
                    RuntimeError, "wide-angle.*{}.*Refresh".format(reason)):
                self.probe(ProbeSocket([payload]))


class CommandTests(unittest.TestCase):
    def test_native_startup_accepts_opt_in_separately_from_control_host(self):
        self.assertIsNone(server.parse_args([]).wide_angle_host)
        options = server.parse_args(["--wide-angle-host", HOST, "--dog-host", "192.168.1.121"])
        self.assertEqual(options.wide_angle_host, HOST)
        self.assertEqual(options.dog_host, "192.168.1.121")
        with contextlib.redirect_stderr(io.StringIO()):
            for flags in (["--wide-angle-host", "example.com"], ["--source", "rtsp"],
                          ["--source", "none", "--wide-angle-host", HOST]):
                with self.subTest(flags=flags), self.assertRaises(SystemExit):
                    server.parse_args(flags)

    def test_launcher_rejects_invalid_host_and_incompatible_backend_before_routing(self):
        for command, backend, host in (
                ("view", "realsense", "192.168.1.120\r\n"),
                ("view", "ros", HOST), ("view", "none", HOST), ("deploy", "none", HOST),
                ("deploy", "realsense", "rtsp://192.168.1.120:8554/test")):
            with self.subTest(command=command, backend=backend), contextlib.redirect_stderr(io.StringIO()):
                with patch.object(client, "resolve_jump", side_effect=AssertionError("routed before validation")):
                    with self.assertRaises(SystemExit) as error:
                        client.main([command, "ysc@192.168.1.103", "--backend", backend,
                                     "--wide-angle-host", host])
                self.assertEqual(error.exception.code, 2)

    def test_launcher_forwards_opt_in_to_detached_and_install_commands(self):
        for command in ("view", "deploy"):
            for extra in ([], ["--wide-angle-host", HOST]):
                with self.subTest(command=command, extra=extra):
                    commands = []
                    with patch.object(client, "show_command", side_effect=commands.append):
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.assertEqual(client.main([command, "ysc@192.168.1.103", "--dry-run", "--direct",
                                                          "--backend", "realsense", *extra]), 0)
                    self.assertEqual(commands[-1][0], "ssh")
                    remote_options = shlex.split(commands[-1][-1])[4:]
                    if extra:
                        start = remote_options.index("--wide-angle-host")
                        self.assertEqual(remote_options[start:start + 2], extra)
                    else:
                        self.assertNotIn("--wide-angle-host", remote_options)
                    if command == "deploy":
                        self.assertIn(("wide_angle.py", "ysc@192.168.1.103:lite3-camera-server/"),
                                      [(Path(item[-2]).name, item[-1]) for item in commands if item[0] == "scp"])


class WorkerTests(unittest.TestCase):
    def test_non_posix_capture_reports_required_pipe_support_without_spawning(self):
        errors = []
        with patch.object(wide_angle.os, "name", "nt"), patch.object(
                wide_angle.subprocess, "Popen", side_effect=AssertionError("unexpected decoder spawn")):
            self.assertEqual(list(wide_angle.capture_frames(HOST, 65, threading.Event(), errors.append)), [])
        self.assertRegex(errors[0], "requires a POSIX host")

    def test_worker_opens_ffmpeg_over_tcp_and_releases_camera_on_read_failure(self):
        cv2 = """
import os
from pathlib import Path

CAP_FFMPEG = 1900
CAP_PROP_OPEN_TIMEOUT_MSEC = 53
CAP_PROP_READ_TIMEOUT_MSEC = 54
IMWRITE_JPEG_QUALITY = 1

class error(Exception):
    pass

class videoio_registry:
    @staticmethod
    def hasBackend(backend):
        return backend == 1900

class VideoCapture:
    def open(self, uri, backend, params):
        assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "rtsp_transport;tcp"
        assert (uri, backend, params) == ("rtsp://192.168.1.120:8554/test", 1900, [53, 2500, 54, 1000])
        self.frames = iter([(True, "frame"), (False, None)])
        return True

    def read(self):
        return next(self.frames)

    def release(self):
        Path("released").touch()

class JPEG(bytes):
    nbytes = 3

def imencode(ext, frame, params):
    assert (ext, frame, params) == (".jpg", "frame", [1, 65])
    return True, JPEG(b"jpg")
"""
        worker = str(Path(wide_angle.__file__).resolve())
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "cv2.py").write_text(cv2, encoding="utf-8")
            environment = dict(os.environ, PYTHONPATH=str(directory))
            completed = subprocess.run([sys.executable, worker, HOST, "65"], cwd=temporary,
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            self.assertEqual(completed.returncode, 4, completed.stderr)
            self.assertEqual(completed.stdout, b"\0\0\0\x03jpg")
            self.assertTrue((directory / "released").is_file())


@unittest.skipUnless(os.name == "posix", "Capture workers require POSIX signals and pipe selectors")
class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.processes = []
        self.errors = []
        self.stop = threading.Event()

    def capture(self, worker):
        original_popen = subprocess.Popen

        def launch(command, **kwargs):
            self.assertEqual(command[0], sys.executable)
            self.assertEqual(Path(command[1]).name, "wide_angle.py")
            self.assertEqual(command[2:], [HOST, "65"])
            process = original_popen([sys.executable, "-c", worker], **kwargs)
            self.processes.append(process)
            self.addCleanup(self.clean_leaked_worker, process)
            return process

        with patch.object(wide_angle.subprocess, "Popen", side_effect=launch):
            yield from wide_angle.capture_frames(HOST, 65, self.stop, self.errors.append)

    @staticmethod
    def clean_leaked_worker(process):
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()

    def assert_reaped(self):
        self.assertEqual(len(self.processes), 1)
        process = self.processes[0]
        self.assertTrue(process.stdout.closed)
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)

    def test_fragmented_frames_and_eof_are_reported_and_reaped(self):
        fixture = struct.pack(">I", 3) + b"one" + struct.pack(">I", 3) + b"two"
        worker = ("import os, time\nfor value in {}:\n"
                  " os.write(1, bytes([value]))\n time.sleep(0.003)\n").format(list(fixture))
        frames = self.capture(worker)
        self.addCleanup(frames.close)
        self.assertEqual(next(frames), b"one")
        self.assertEqual(next(frames), b"two")
        self.assertEqual(list(frames), [])
        self.assertRegex(self.errors[0], "decoder.*ended")
        self.assert_reaped()

    def test_zero_length_and_oversized_frames_are_reported_without_waiting_for_body(self):
        for length in (0, 8 * 1024 * 1024 + 1):
            with self.subTest(length=length):
                self.processes.clear()
                self.errors.clear()
                worker = "import os, struct, time; os.write(1, struct.pack('>I', {})); time.sleep(10)".format(length)
                self.assertEqual(list(self.capture(worker)), [])
                self.assertRegex(self.errors[0], "invalid.*length")
                self.assert_reaped()

    def test_worker_exit_status_explains_unavailable_support_open_and_read_errors(self):
        for status, reason in ((2, "OpenCV"), (3, "open"), (4, "read"), (5, "JPEG")):
            with self.subTest(status=status):
                self.processes.clear()
                self.errors.clear()
                self.assertEqual(list(self.capture("raise SystemExit({})".format(status))), [])
                self.assertRegex(self.errors[0], reason)
                self.assert_reaped()

    def test_partial_startup_and_interframe_stalls_observe_full_frame_deadlines(self):
        for initial, reason in ((b"", "first"), (struct.pack(">I", 3) + b"one", "next")):
            with self.subTest(reason=reason):
                self.processes.clear()
                self.errors.clear()
                worker = ("import os, time; os.write(1, {}); time.sleep(10)"
                          .format(repr(initial + struct.pack(">I", 10) + b"partial")))
                with patch.object(wide_angle, "FIRST_FRAME_TIMEOUT", 0.35), patch.object(
                        wide_angle, "NEXT_FRAME_TIMEOUT", 0.15):
                    self.assertEqual(list(self.capture(worker)), [b"one"] if initial else [])
                self.assertRegex(self.errors[0], "timed out.*{}.*frame".format(reason))
                self.assert_reaped()

    def test_stop_kills_worker_ignoring_terminate_and_reaps_it(self):
        worker = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "os.write(1, b'\\0\\0\\0\\x05ready')\n"
            "time.sleep(10)\n"
        )
        frames = self.capture(worker)
        self.addCleanup(frames.close)
        self.assertEqual(next(frames), b"ready")
        timer = threading.Timer(0.025, self.stop.set)
        timer.start()
        self.addCleanup(timer.join)
        start = time.monotonic()
        self.assertEqual(list(frames), [])
        self.assertLess(time.monotonic() - start, 1.2)
        self.assertEqual(self.processes[0].returncode, -signal.SIGKILL)
        self.assertEqual(self.errors, [])
        self.assert_reaped()

    def test_reported_failure_precedes_reap_and_controller_waits_before_switching(self):
        killed = threading.Event()
        allow_reap = threading.Event()
        released = threading.Event()
        arm_opened = threading.Event()
        opened = []
        reader, writer = os.pipe()
        os.write(writer, b"\0\0\0\0")
        os.close(writer)

        class UnreapedDecoder:
            stdout = os.fdopen(reader, "rb", buffering=0)

            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                killed.set()

            def wait(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired("wide-angle-worker", timeout)
                if not allow_reap.wait(5):
                    raise AssertionError("The test never released simulated process reaping")
                released.set()

        process = UnreapedDecoder()
        self.addCleanup(process.stdout.close)
        self.addCleanup(allow_reap.set)

        def local_frames(options, stop):
            opened.append(options.device)
            if options.device == "/dev/video8":
                arm_opened.set()
            yield b"local-frame"
            stop.wait()

        def local_sources():
            return [{"id": "front", "kind": "d435i", "device": "/dev/video4",
                     "label": "D435i RGB", "pixel_format": "YUYV"},
                    {"id": "arm", "kind": "sxw", "device": "/dev/video8",
                     "label": "Arm camera", "pixel_format": "MJPG"}]

        options = server.parse_args(["--source", "v4l2", "--device", "/dev/video4",
                                     "--wide-angle-host", HOST, "--retry-camera"])
        controller = server.CaptureController(options, discover=local_sources, stop_timeout=0.1)
        with patch.object(wide_angle.subprocess, "Popen", return_value=process) as launch, \
                patch.object(wide_angle, "probe"), patch.object(server, "v4l2_frames", local_frames), \
                patch.object(server, "discover_sources", local_sources), contextlib.redirect_stderr(io.StringIO()):
            try:
                controller.start()
                wide = controller.catalog()["sources"][-1]["id"]
                controller.select(wide)
                self.assertTrue(killed.wait(1))
                decoder_thread = controller.producer
                self.assertTrue(decoder_thread.is_alive())
                self.assertTrue(process.stdout.closed)
                health, frame, _ = controller.frames.snapshot()
                self.assertEqual(health["status"], "error")
                self.assertIn("invalid JPEG length", health["error"])
                self.assertIsNone(frame)
                self.assertFalse(released.is_set())
                with self.assertRaisesRegex(RuntimeError, "previous camera did not stop"):
                    controller.select("arm")
                self.assertIs(controller.producer, decoder_thread)
                self.assertEqual(opened, ["/dev/video4"])
                self.assertEqual(launch.call_count, 1)

                allow_reap.set()
                decoder_thread.join(1)
                self.assertFalse(decoder_thread.is_alive())
                self.assertTrue(released.is_set())
                self.assertEqual(controller.select("arm")["selected"], "arm")
                self.assertTrue(arm_opened.wait(1))
                self.assertEqual(opened, ["/dev/video4", "/dev/video8"])
            finally:
                allow_reap.set()
                controller.shutdown()

    def test_shutdown_returns_bounded_failure_but_process_waits_for_rtsp_owner(self):
        code = """
import sys
import threading
from unittest.mock import patch
from src import server, wide_angle

started = threading.Event()

def capture(options, frames, stop):
    if options.source == "rtsp":
        started.set()
        stop.wait()
        # Simulate the decoder owner waiting for the kernel to reap its child.
        sys.stdin.read(1)
        print("decoder reaped", flush=True)
    else:
        stop.wait()

options = server.parse_args(["--wide-angle-host", "192.168.1.120"])
controller = server.CaptureController(options, discover=lambda: [], capture_fn=capture, stop_timeout=0.05)
with patch.object(wide_angle, "probe"):
    controller.start()
    assert controller.producer.daemon
    wide_id = controller.catalog()["sources"][0]["id"]
    controller.select(wide_id)
    assert started.wait(1)
    assert not controller.shutdown()
    print("shutdown timed out", flush=True)
"""
        checkout = Path(__file__).resolve().parents[1]
        with subprocess.Popen([sys.executable, "-B", "-c", code], cwd=str(checkout), text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as server_process:
            message = Queue()
            reading = threading.Thread(target=lambda: message.put(server_process.stdout.readline()), daemon=True)
            reading.start()
            try:
                self.assertEqual(message.get(timeout=3).strip(), "shutdown timed out")
                with self.assertRaises(subprocess.TimeoutExpired):
                    server_process.wait(timeout=0.2)
                output, error = server_process.communicate(input="r", timeout=3)
                self.assertEqual(server_process.returncode, 0, error)
                self.assertEqual(output.strip(), "decoder reaped")
            finally:
                if server_process.poll() is None:
                    server_process.kill()
                reading.join(3)


if __name__ == "__main__":
    unittest.main()
