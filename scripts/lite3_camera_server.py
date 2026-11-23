#!/usr/bin/env python3
"""Serve Lite3 camera frames on the loopback address. Use an SSH tunnel for remote access."""

import argparse
import contextlib
import itertools
import json
from pathlib import Path
import re
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer, ThreadingMixIn


LOOPBACK = "127.0.0.1"
BOUNDARY = b"--frame\r\n"
SOURCE_LABELS = {"demo": "Synthetic demo", "realsense": "RealSense", "v4l2": "V4L2 RGB"}

ASSET_DIR = Path(__file__).resolve().parent / "assets"
PAGE = (Path(__file__).resolve().parent / "web" / "camera.html").read_text(encoding="utf-8")


class LatestFrame:
    """Share the latest frame between the capture thread and all viewer requests."""

    def __init__(self, source, stale_after=2.0):
        self.source = source
        self.stale_after = stale_after
        self._changed = threading.Condition()
        self._frame = None
        self._captured_at = None
        self._sequence = 0
        self._generation = 0
        self._error = None

    def _age(self):
        if self._captured_at is not None:
            return time.monotonic() - self._captured_at
        return None

    def publish(self, jpeg):
        with self._changed:
            age = self._age()
            if self._error or (age is not None and age >= self.stale_after):
                self._generation += 1
            self._frame = jpeg
            self._captured_at = time.monotonic()
            self._error = None
            self._sequence += 1
            self._changed.notify_all()

    def fail(self, message):
        with self._changed:
            changed = message != self._error
            self._error = message
            self._changed.notify_all()
            return changed

    def snapshot(self):
        with self._changed:
            age = self._age()
            error = self._error
            if error:
                status = "error"
            elif age is None:
                status = "starting"
            elif age >= self.stale_after:
                status = "stale"
                error = "No recent frames. The server will wait for video."
            else:
                status = "live"
            health = {
                "source": self.source,
                "status": status,
                "frame_age_s": None if age is None else round(age, 3),
                "error": error,
                "generation": self._generation,
            }
            return health, self._frame if status == "live" else None, self._sequence

    def wait_for_frame(self, sequence, generation):
        with self._changed:
            while not self._error and generation == self._generation:
                age = self._age()
                if age is None or age >= self.stale_after:
                    break
                if sequence != self._sequence:
                    return self._frame, self._sequence
                self._changed.wait(self.stale_after - age)
            return None, sequence


class CameraHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    timeout = 0.2

    def __init__(self, port, frames):
        self.frames = frames
        super().__init__((LOOPBACK, port), CameraRequestHandler)

    def server_bind(self):
        # HTTPServer resolves its address to a hostname. DNS can delay this call, even for loopback.
        # This server needs only its numeric local address.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class CameraRequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def log_message(self, *args):
        # Omit request logs to keep repeated health checks out of the SSH terminal.
        pass

    def do_GET(self):
        try:
            self._get()
        except (ConnectionError, socket.timeout):
            # A tab close or reload can interrupt a write.
            pass

    def _get(self):
        path = self.path.partition("?")[0]
        frames = self.server.frames
        if path == "/":
            source_label = SOURCE_LABELS[frames.source]
            page = PAGE.replace("__SOURCE_LABEL__", source_label).encode()
            self._respond(200, "text/html; charset=utf-8", page)
            return
        if path not in ("/healthz", "/snapshot.jpg", "/stream.mjpg"):
            self._respond(404, "text/plain; charset=utf-8", b"Not found\n")
            return

        health, frame, sequence = frames.snapshot()
        if path == "/healthz" or frame is None:
            status = 200 if frame is not None else 503
            self._respond(status, "application/json", json.dumps(health).encode())
        elif path == "/snapshot.jpg":
            self._respond(200, "image/jpeg", frame)
        else:
            self._stream(frame, sequence, health["generation"])

    def _headers(self, status, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")

    def _respond(self, status, content_type, body):
        self._headers(status, content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, frame, sequence, generation):
        self._headers(200, "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        while frame is not None:
            headers = "Content-Type: image/jpeg\r\nContent-Length: {}\r\n\r\n".format(len(frame))
            self.wfile.write(BOUNDARY + headers.encode("ascii") + frame + b"\r\n")
            frame, sequence = self.server.frames.wait_for_frame(sequence, generation)
        self.wfile.write(b"--frame--\r\n")


def demo_frames(options, stop):
    frames = [(ASSET_DIR / "demo" / "frame-{:02d}.jpg".format(index)).read_bytes()
              for index in range(1, 5)]
    animation = frames + frames[-2:0:-1]
    frames_per_step = max(1, options.fps // 4)
    for index in itertools.count():
        if stop.is_set():
            break
        yield animation[(index // frames_per_step) % len(animation)]
        stop.wait(1.0 / options.fps)


def _report_capture_error(frames, message):
    if frames.fail(message):
        print(message, file=sys.stderr, flush=True)


def encode_jpeg(cv2, image, quality):
    ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return jpeg.tobytes()


def realsense_frames(options, frames, stop):
    try:
        import pyrealsense2 as rs
        import numpy as np
        import cv2
    except ImportError as error:
        raise RuntimeError(
            "Cannot load the RealSense Python dependencies (pyrealsense2, numpy, cv2): {}".format(error)
        ) from error

    config = rs.config()
    if options.serial:
        config.enable_device(options.serial)
    config.enable_stream(rs.stream.color, options.width, options.height, rs.format.bgr8, options.fps)
    if options.depth:
        config.enable_stream(rs.stream.depth, options.width, options.height, rs.format.z16, options.fps)
        colorizer = rs.colorizer()

    pipeline = rs.pipeline()
    try:
        pipeline.start(config)
    except RuntimeError as error:
        raise RuntimeError(
            "Cannot start RealSense. Check the device connection, other camera users, and stream profile. "
            "{}".format(error)
        ) from error
    try:
        while not stop.is_set():
            try:
                captured = pipeline.wait_for_frames(1000)
                color = captured.get_color_frame()
                if not color:
                    raise RuntimeError("The camera returned no RGB frame")
                image = np.asanyarray(color.get_data())
                if options.depth:
                    depth = captured.get_depth_frame()
                    if not depth:
                        raise RuntimeError("The camera returned no depth frame")
                    depth_rgb = np.asanyarray(colorizer.colorize(depth).get_data())
                    depth_bgr = cv2.cvtColor(depth_rgb, cv2.COLOR_RGB2BGR)
                    image = np.hstack((image, depth_bgr))
                jpeg = encode_jpeg(cv2, image, options.quality)
            except Exception as error:
                _report_capture_error(frames, "Cannot read the RealSense frame: {}".format(error))
                stop.wait(0.25)
                continue
            if not stop.is_set():
                yield jpeg
    finally:
        pipeline.stop()


def v4l2_frames(options, stop):
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError("Cannot load OpenCV (cv2) for V4L2: {}".format(error)) from error

    device = options.device
    camera = cv2.VideoCapture(device, cv2.CAP_V4L2)
    try:
        if not camera.isOpened():
            raise RuntimeError("Cannot open V4L2 device {}. Check the device connection, other camera users, and permissions.".format(device))
        settings = (
            ("pixel format", cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV")),
            ("width", cv2.CAP_PROP_FRAME_WIDTH, options.width),
            ("height", cv2.CAP_PROP_FRAME_HEIGHT, options.height),
            ("frame rate", cv2.CAP_PROP_FPS, options.fps),
        )
        for name, prop, value in settings:
            if not camera.set(prop, value):
                raise RuntimeError("Cannot set {} on V4L2 device {}".format(name, device))
        while not stop.is_set():
            ok, image = camera.read()
            if not ok or image is None:
                raise RuntimeError("Cannot read a frame from V4L2 device {}".format(device))
            if not stop.is_set():
                yield encode_jpeg(cv2, image, options.quality)
    finally:
        camera.release()


def capture(options, frames, stop):
    if options.source == "demo":
        source = demo_frames(options, stop)
    elif options.source == "v4l2":
        source = v4l2_frames(options, stop)
    else:
        source = realsense_frames(options, frames, stop)
    try:
        with contextlib.closing(source):
            for jpeg in source:
                frames.publish(jpeg)
    except Exception as error:
        _report_capture_error(frames, str(error) or type(error).__name__)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=SOURCE_LABELS, default="realsense")
    parser.add_argument("--port", type=int, default=8080, help="loopback port (use 0 for a free port)")
    parser.add_argument("--width", type=int, default=640, help="frame width in pixels for each stream")
    parser.add_argument("--height", type=int, default=480, help="frame height in pixels for each stream")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--quality", type=int, default=70, help="camera JPEG quality (1 to 100)")
    parser.add_argument("--depth", action="store_true", help="show RealSense depth with false colors beside RGB")
    parser.add_argument("--serial", help="select a RealSense camera by serial number")
    parser.add_argument("--device", help="V4L2 RGB device path (required for --source v4l2)")
    options = parser.parse_args(argv)
    if options.source == "v4l2":
        if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9_][A-Za-z0-9_.:+-]*)", options.device or ""):
            parser.error("--source v4l2 requires --device /dev/videoN or /dev/v4l/by-id/NAME")
        if options.depth or options.serial is not None:
            parser.error("--source v4l2 does not support --depth or --serial")
    elif options.device is not None:
        parser.error("--device requires --source v4l2")
    if not 0 <= options.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if min(options.width, options.height, options.fps) <= 0:
        parser.error("--width, --height and --fps must be positive")
    if not 1 <= options.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    return options


def main(argv=None):
    options = parse_args(argv)
    frames = LatestFrame(options.source)
    stop = threading.Event()
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    try:
        server = CameraHTTPServer(options.port, frames)
    except OSError as error:
        print("Cannot listen on {}:{}: {}".format(LOOPBACK, options.port, error), file=sys.stderr)
        return 1

    producer = threading.Thread(target=capture, args=(options, frames, stop), daemon=True)
    with server:
        producer.start()
        try:
            print("http://{}:{}/".format(LOOPBACK, server.server_port), flush=True)
            while not stopping:
                server.handle_request()
        finally:
            stop.set()
            frames.fail("The server stopped")
    producer.join(timeout=3)
    if producer.is_alive():
        print("Camera shutdown timed out", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
