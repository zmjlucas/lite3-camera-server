#!/usr/bin/env python3
"""Lite3 development server: shared cameras and bounded robot communications."""

import argparse
import contextlib
import errno
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import TCPServer, ThreadingMixIn


DEFAULT_BIND = "0.0.0.0"
BOUNDARY = b"--frame\r\n"
SOURCE_LABELS = {"realsense": "RealSense", "v4l2": "V4L2 RGB", "none": "No camera"}

PAGE = (Path(__file__).resolve().parent / "web" / "camera.html").read_text(encoding="utf-8")


def v4l2_formats(device):
    """Read Linux V4L2 capabilities/formats without configuring or starting capture."""
    import fcntl  # Keep the module importable on Windows and macOS.

    descriptor = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    try:
        # Linux videodev2.h: v4l2_capability (104 bytes), v4l2_fmtdesc (64 bytes).
        capabilities = bytearray(104)
        fcntl.ioctl(descriptor, 0x80685600, capabilities)  # VIDIOC_QUERYCAP
        caps, device_caps = struct.unpack_from("=II", capabilities, 84)
        if caps & 0x80000000:  # V4L2_CAP_DEVICE_CAPS: use this node, not the parent union.
            caps = device_caps
        if not caps & 0x00000001 or not caps & 0x04000000:  # VIDEO_CAPTURE and STREAMING
            return set()
        formats = set()
        for index in range(256):
            description = bytearray(64)
            struct.pack_into("=II", description, 0, index, 1)  # VIDEO_CAPTURE buffer type
            try:
                fcntl.ioctl(descriptor, 0xc0405602, description)  # VIDIOC_ENUM_FMT
            except OSError as error:
                if error.errno == errno.EINVAL:
                    break
                raise
            fourcc = struct.unpack_from("=I", description, 44)[0]
            formats.add(struct.pack("<I", fourcc).decode("ascii", errors="replace"))
        if formats and not os.access(device, os.R_OK | os.W_OK):
            raise PermissionError(errno.EACCES, "Capture requires read/write access", device)
        return formats
    finally:
        os.close(descriptor)


def camera_device_owners(proc_root):
    """Best-effort read-only scan; never treat our shared capture as a conflict."""
    owners = {}
    try:
        processes = list(Path(proc_root).iterdir())
    except OSError:
        return owners
    for process in processes:
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            descriptors = list((process / "fd").iterdir())
        except OSError:
            continue  # Process exited or procfs denies access to its descriptors.
        for descriptor in descriptors:
            try:
                device = os.readlink(str(descriptor))
            except OSError:
                continue
            if not re.fullmatch(r"/dev/(video[0-9]+|bus/usb/[0-9]+/[0-9]+)", device):
                continue
            try:
                name = Path(os.readlink(str(process / "exe"))).name
            except OSError:
                try:
                    name = (process / "comm").read_text(errors="replace").strip()
                except OSError:
                    name = "another process"
            owners[device] = "{} (PID {})".format(name, process.name)
    return owners


def sysfs_rgb_node(node, usb, kind):
    """Identify an inaccessible RGB node without guessing its /dev/video number."""
    if (node / "index").read_text().strip() != "0":
        return False  # UVC metadata has a separate index on the same interface.
    if kind == "sxw":
        return True
    names = [(node / "name").read_text()]
    for parent in node.resolve().parents:
        if parent == usb:
            break
        if (parent / "interface").is_file():
            names.append((parent / "interface").read_text())
    return any(re.search(r"\brgb\b", name, re.I) for name in names)


def discover_sources(sysfs_root=Path("/sys/class/video4linux"), probe=None, proc_root=Path("/proc")):
    """Identify supported RGB nodes through their USB parent and capture formats."""
    probe = probe or v4l2_formats
    root = Path(sysfs_root)
    try:
        nodes = [node for node in root.iterdir() if re.fullmatch(r"video[0-9]+", node.name)]
    except FileNotFoundError:
        return []
    owners = camera_device_owners(proc_root)
    found = []
    for node in sorted(nodes, key=lambda item: int(item.name[5:])):
        try:
            resolved = node.resolve()
            usb = next((parent for parent in resolved.parents if (parent / "idVendor").is_file()), None)
            if usb is None:
                continue
            vendor = (usb / "idVendor").read_text().strip().lower()
            product_id = (usb / "idProduct").read_text().strip().lower()
            product = (usb / "product").read_text().strip() if (usb / "product").is_file() else ""
            serial = (usb / "serial").read_text().strip() if (usb / "serial").is_file() else ""
            if (vendor, product_id) == ("8086", "0b3a") or (
                    "realsense" in product.lower() and re.search(r"435i\b", product, re.I)):
                kind, label, pixel_format = "d435i", "D435i RGB", "YUYV"
            elif (vendor, product_id) == ("0bda", "525a"):
                kind, label, pixel_format = "sxw", "Arm camera (SXW USB Camera)", "MJPG"
            else:
                continue
            device = "/dev/" + node.name
            error = None
            try:
                if pixel_format not in probe(device):
                    continue
            except (OSError, ImportError) as failure:
                if not sysfs_rgb_node(node, usb, kind):
                    continue
                error = "Cannot inspect {}: {}. Check camera usage, connection and permissions; then Refresh.".format(device, failure)
            # SDK capture can own a sibling depth interface or the USB device itself.
            related = ["/dev/" + sibling.name for sibling in nodes if usb in sibling.resolve().parents]
            try:
                related.append("/dev/bus/usb/{:03d}/{:03d}".format(
                    int((usb / "busnum").read_text()), int((usb / "devnum").read_text())))
            except (OSError, ValueError):
                pass
            busy = next((path for path in related if path in owners), None)
            if busy:
                error = "Camera is in use by {} ({}). Release it in that application, then Refresh.".format(owners[busy], busy)
            identity = "\0".join((str(resolved), vendor, product_id, serial))
            source_id = "camera-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            found.append({"id": source_id, "label": device + " — " + label, "device": device,
                          "backend": "v4l2", "kind": kind, "serial": serial,
                          "usb_path": str(usb), "pixel_format": pixel_format,
                          "available": error is None, "error": error})
        except (OSError, ImportError):
            # An inaccessible/disconnected node must not hide other supported cameras.
            continue
    return found


class LatestFrame:
    """Share the latest frame between the capture thread and all viewer requests."""

    def __init__(self, source, stale_after=2.0, source_id=None):
        self.source = source
        self.source_id = source_id
        self.stale_after = stale_after
        self._changed = threading.Condition()
        self._frame = None
        self._captured_at = None
        self._sequence = 0
        self._generation = 0
        self._error = None
        self._retired = False

    def _age(self):
        if self._captured_at is not None:
            return time.monotonic() - self._captured_at
        return None

    def publish(self, jpeg):
        with self._changed:
            if self._retired:
                return
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
            if self._retired:
                return False
            changed = message != self._error
            self._error = message
            self._changed.notify_all()
            return changed

    def retire(self, message):
        """End this capture generation permanently, including any late producer writes."""
        with self._changed:
            self._retired = True
            self._frame = None
            self._error = message
            self._generation += 1
            self._changed.notify_all()

    def identify(self, source_id):
        """Bind SDK-discovered identity before the first frame is published."""
        with self._changed:
            if not self._retired and self._sequence == 0:
                self.source_id = source_id

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
                "source_id": self.source_id,
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

    def __init__(self, port, frames, controller=None, bind=DEFAULT_BIND,
                 control=None, control_token=None, arm_gateway=None):
        self.control = control
        self.control_token = control_token
        self.arm_gateway = arm_gateway
        self._frames = frames
        self.controller = controller
        super().__init__((bind, port), CameraRequestHandler)

    @property
    def frames(self):
        return self.controller.frames if self.controller is not None else self._frames

    def server_bind(self):
        # HTTPServer resolves its address to a hostname. DNS can delay this call, even for loopback.
        # This server needs only its numeric local address.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class CameraRequestHandler(BaseHTTPRequestHandler):
    def version_string(self):
        return "Lite3Dev"

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
        if not self._same_origin():
            return
        path = self.path.partition("?")[0]
        if path == "/api/status":
            control = getattr(self.server, "control", None)
            self._json(200, control.status() if control else {"mode": "disabled"})
            return
        if path == "/control":
            page = (Path(__file__).resolve().parent / "web" / "control.html").read_bytes()
            self._respond(200, "text/html; charset=utf-8", page)
            return
        frames = self.server.frames
        if path == "/":
            source_label = SOURCE_LABELS[frames.source]
            page = PAGE.replace("__SOURCE_LABEL__", source_label).encode()
            self._respond(200, "text/html; charset=utf-8", page)
            return
        if path not in ("/healthz", "/snapshot.jpg", "/stream.mjpg", "/sources"):
            self._respond(404, "text/plain; charset=utf-8", b"Not found\n")
            return

        if path == "/sources":
            try:
                controller = getattr(self.server, "controller", None)
                catalog = controller.catalog() if controller else {"sources": [], "selected": None}
                self._json(200, catalog)
            except (OSError, RuntimeError) as error:
                self._json(503, {"error": "Cannot discover camera sources: " + str(error)})
            return

        health, frame, sequence = frames.snapshot()
        if path == "/healthz" or frame is None:
            status = 200 if frame is not None else 503
            self._respond(status, "application/json", json.dumps(health).encode())
        elif path == "/snapshot.jpg":
            self._respond(200, "image/jpeg", frame)
        else:
            self._stream(frames, frame, sequence, health["generation"])

    def _same_origin(self):
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0].lower() != "http://" + self.headers.get("Host", "").lower()):
            self._respond(403, "text/plain; charset=utf-8", b"Same origin required\n")
            return False
        return True

    def do_POST(self):
        try:
            self._post()
        except (ConnectionError, socket.timeout):
            pass

    def _post(self):
        if not self._same_origin():
            return
        if self.path.partition("?")[0].startswith("/api/"):
            self._control_post()
            return
        if self.path.partition("?")[0] != "/source":
            self._json(404, {"error": "Not found"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]) or self.headers.get_all("Transfer-Encoding"):
            self._json(400, {"error": "One valid Content-Length is required"})
            return
        size = int(lengths[0])
        if size > 4096:
            self._json(413, {"error": "Selection request is too large"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(400, {"error": "Use an application/json selection request"})
            return
        try:
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("Incomplete selection request")
            selection = json.loads(body.decode("utf-8"))
            if (not isinstance(selection, dict) or set(selection) != {"id"}
                    or not isinstance(selection["id"], str) or not 1 <= len(selection["id"]) <= 128):
                raise ValueError("Select a camera id from the current source list")
            controller = getattr(self.server, "controller", None)
            if controller is None:
                raise RuntimeError("Camera selection is unavailable in this server")
            catalog = controller.select(selection["id"])
        except (ValueError, UnicodeError, RecursionError) as error:
            self._json(400, {"error": str(error)})
        except (OSError, RuntimeError) as error:
            self._json(409, {"error": str(error)})
        else:
            self._json(200, catalog)

    def _control_post(self):
        token = getattr(self.server, "control_token", None)
        credentials = self.headers.get_all("Authorization", [])
        if (not token or len(credentials) != 1
                or not hmac.compare_digest(credentials[0].encode("utf-8"),
                                           ("Bearer " + token).encode("utf-8"))):
            self._json(401, {"error": "A configured control bearer token is required"})
            return
        action = self.path.partition("?")[0][5:]
        allowed = {"control/acquire", "control/heartbeat", "control/release", "control/estop",
                   "control/reset", "dog/stand", "dog/velocity", "dog/stop", "arm/jog",
                   "arm/state", "arm/reconnect"}
        if action not in allowed:
            self._json(404, {"error": "Unknown control operation"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0])
                or self.headers.get_all("Transfer-Encoding")):
            self._json(400, {"error": "One valid Content-Length is required"})
            return
        size = int(lengths[0])
        if size > 4096:
            self._json(413, {"error": "Control request is too large"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(400, {"error": "Use application/json"})
            return
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON field")
                result[key] = value
            return result
        def reject_constant(value):
            raise ValueError("Non-finite JSON number")
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("Incomplete request body")
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=reject_constant)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            control = getattr(self.server, "control", None)
            if control is None:
                self._json(503, {"error": "Control is disabled"})
                return
            if action in ("arm/state", "arm/reconnect"):
                if body:
                    raise ValueError("This operation takes an empty object")
                gateway = getattr(self.server, "arm_gateway", None)
                if gateway is None:
                    state = control.status()
                    if state.get("mode") == "disabled":
                        self._json(409, {"error": "Control is disabled"})
                        return
                    result = dict(state.get("arm", {}), observed=False)
                else:
                    result = gateway.read_state() if action == "arm/state" else gateway.reconnect()
            else:
                result = control.dispatch(action, body)
            self._json(200, result)
        except (ValueError, UnicodeError, RecursionError) as error:
            self._json(getattr(error, "status", 400), {"error": str(error)})
        except (OSError, RuntimeError) as error:
            self._json(getattr(error, "status", 503), {"error": str(error)})

    def _json(self, status, value):
        self._respond(status, "application/json", json.dumps(value).encode())

    def _headers(self, status, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Lite3-Camera-Protocol", "1")
        self.send_header("X-Lite3-Dev-Protocol", "1")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

    def _respond(self, status, content_type, body):
        self._headers(status, content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, frames, frame, sequence, generation):
        self._headers(200, "multipart/x-mixed-replace; boundary=frame")
        self.send_header("X-Camera-Source", frames.source)
        self.send_header("X-Camera-Source-Id", frames.source_id or "")
        self.end_headers()
        while frame is not None:
            headers = "Content-Type: image/jpeg\r\nContent-Length: {}\r\n\r\n".format(len(frame))
            self.wfile.write(BOUNDARY + headers.encode("ascii") + frame + b"\r\n")
            frame, sequence = frames.wait_for_frame(sequence, generation)
        self.wfile.write(b"--frame--\r\n")


def _report_capture_error(frames, message):
    if frames.fail(message):
        print(message, file=sys.stderr, flush=True)


def encode_jpeg(cv2, image, quality):
    ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return jpeg.tobytes()


def _physical_port_matches(physical_port, usb_path):
    if not physical_port or not usb_path:
        return False
    port, parent = Path(physical_port), Path(usb_path)
    return port == parent or parent in port.parents


def _realsense_physical_port(rs, device):
    try:
        return device.get_info(rs.camera_info.physical_port)
    except RuntimeError:
        return None


def realsense_serial_for_source(rs, usb_path, serial=None):
    """Map a Linux USB parent to its SDK serial; sysfs USB serials are not SDK IDs."""
    matches = set()
    for device in rs.context().query_devices():
        if not _physical_port_matches(_realsense_physical_port(rs, device), usb_path):
            continue
        try:
            sdk_serial = device.get_info(rs.camera_info.serial_number)
        except RuntimeError:
            continue
        if sdk_serial and (serial is None or sdk_serial == serial):
            matches.add(sdk_serial)
    if len(matches) != 1:
        raise RuntimeError("Cannot uniquely match the selected USB camera to a RealSense SDK device. "
                           "Check the connection and any explicit --serial option.")
    return next(iter(matches))


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
    usb_path = getattr(options, "usb_path", None)
    serial = realsense_serial_for_source(rs, usb_path, options.serial) if usb_path else options.serial
    if serial:
        config.enable_device(serial)
    config.enable_stream(rs.stream.color, options.width, options.height, rs.format.bgr8, options.fps)
    if options.depth:
        config.enable_stream(rs.stream.depth, options.width, options.height, rs.format.z16, options.fps)
        colorizer = rs.colorizer()

    pipeline = rs.pipeline()
    try:
        profile = pipeline.start(config)
    except RuntimeError as error:
        raise RuntimeError(
            "Cannot start RealSense. Check the device connection, other camera users, and stream profile. "
            "{}".format(error)
        ) from error
    try:
        physical_port = _realsense_physical_port(rs, profile.get_device())
        if usb_path and not _physical_port_matches(physical_port, usb_path):
            raise RuntimeError("The RealSense SDK started a different physical camera; capture was stopped.")
        matching = [entry["id"] for entry in getattr(options, "camera_sources", [])
                    if entry["kind"] == "d435i" and _physical_port_matches(physical_port, entry["usb_path"])]
        frames.identify(matching[0] if len(matching) == 1 else None)
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
            ("pixel format", cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*getattr(options, "pixel_format", "YUYV"))),
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
    while not stop.is_set():
        try:
            if options.retry_camera and options.source == "v4l2":
                # A supported USB camera may appear after the initial boot-time scan.
                # Refresh its format and identity, but continue opening the configured
                # by-id path so device renumbering cannot redirect capture to another node.
                try:
                    detected = discover_sources()
                except (OSError, RuntimeError):
                    detected = []
                matching = [entry for entry in detected
                            if os.path.realpath(entry["device"]) == os.path.realpath(options.device)]
                if len(matching) == 1:
                    options.pixel_format = matching[0]["pixel_format"]
                    frames.identify(matching[0]["id"])
            source = (v4l2_frames(options, stop) if options.source == "v4l2"
                      else realsense_frames(options, frames, stop))
            with contextlib.closing(source):
                for jpeg in source:
                    frames.publish(jpeg)
        except Exception as error:
            _report_capture_error(frames, str(error) or type(error).__name__)
        # A boot service can start before USB enumeration finishes. Keep HTTP available
        # and retry capture; Event.wait also lets source changes and shutdown stop promptly.
        if not options.retry_camera or stop.wait(5):
            return


class CaptureController:
    """Own one capture at a time while preserving the shared HTTP server."""

    def __init__(self, options, discover=None, capture_fn=None, stop_timeout=3.0):
        self.options = options
        self.discover = discover or discover_sources
        self.capture_fn = capture_fn or capture
        self.stop_timeout = stop_timeout
        self.frames = LatestFrame(options.source)
        self.producer = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._closed = False

    def _catalog(self, detected):
        public = []
        for entry in detected:
            public.append({"id": entry["id"], "label": entry["label"], "device": entry["device"],
                           "backend": self._backend(entry), "available": entry.get("available", True),
                           "error": entry.get("error")})
        source_id = self.frames.snapshot()[0]["source_id"]
        selected = source_id if any(entry["id"] == source_id for entry in detected) else None
        return {"sources": public, "selected": selected}

    def _backend(self, entry):
        return "realsense" if entry["kind"] == "d435i" and self.options.source == "realsense" else "v4l2"

    def _source_options(self, entry):
        options = argparse.Namespace(**vars(self.options))
        options.source = self._backend(entry)
        if options.source == "realsense":
            options.device = None
            options.usb_path = entry["usb_path"]
        else:
            options.device = entry["device"]
            options.depth = False
            options.serial = None
            options.pixel_format = entry["pixel_format"]
        return options

    def _launch(self, options, selected, detected):
        options = argparse.Namespace(**vars(options))
        options.camera_sources = detected
        self.frames = LatestFrame(options.source, source_id=selected)
        self._stop = threading.Event()
        self.producer = threading.Thread(target=self.capture_fn,
                                         args=(options, self.frames, self._stop), daemon=True)
        self.producer.start()

    def start(self):
        with self._lock:
            if self.producer is not None or self._closed:
                return
            try:
                detected = self.discover()
            except (OSError, RuntimeError):
                detected = []  # Explicit CLI capture remains available if discovery fails.
            if self.options.source == "v4l2":
                matching = [entry for entry in detected
                            if os.path.realpath(entry["device"]) == os.path.realpath(self.options.device)]
            else:
                matching = [entry for entry in detected if entry["kind"] == "d435i"
                            and not self.options.serial]
            if len(matching) == 1:
                initial = self._source_options(matching[0])
                if self.options.source == "v4l2":
                    initial.device = self.options.device
                self._launch(initial, matching[0]["id"], detected)
            else:
                self._launch(self.options, None, detected)

    def catalog(self):
        with self._lock:
            return self._catalog(self.discover())

    def select(self, source_id):
        with self._lock:
            if self._closed:
                raise RuntimeError("The camera server is stopping")
            detected = self.discover()
            entry = next((item for item in detected if item["id"] == source_id), None)
            if entry is None:
                raise ValueError("That camera is unavailable. Refresh the source list.")
            if not entry.get("available", True):
                raise RuntimeError(entry.get("error") or "That camera is unavailable. Refresh the source list.")
            if (source_id == self.frames.snapshot()[0]["source_id"] and self.producer is not None
                    and self.producer.is_alive() and not self._stop.is_set()
                    and self.frames.snapshot()[0]["status"] in ("live", "starting")):
                return self._catalog(detected)
            options = self._source_options(entry)
            self.frames.retire("Camera switching; the previous video has ended")
            self._stop.set()
            if self.producer is not None:
                self.producer.join(self.stop_timeout)
                if self.producer.is_alive():
                    message = "The previous camera did not stop. No new camera was opened; restart the shared server if it remains blocked."
                    self.frames.retire(message)
                    raise RuntimeError(message)
            self._launch(options, entry["id"], detected)
            return self._catalog(detected)

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._stop.set()
            self.frames.retire("The server stopped")
            if self.producer is not None:
                self.producer.join(self.stop_timeout)
                return not self.producer.is_alive()
            return True


def load_control_token(path):
    if not path:
        return None
    with open(path, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Control token must be a regular file")
        if os.name != "nt" and info.st_mode & 0o077:
            raise ValueError("Control token file must be private (chmod 600)")
        token = handle.read(513).decode("ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Control token must contain 32-128 URL-safe characters")
    return token


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=SOURCE_LABELS, default="realsense")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (use 0 for a free port)")
    parser.add_argument("--bind", default=DEFAULT_BIND, help="listen address (default: all IPv4 interfaces)")
    parser.add_argument("--width", type=int, default=640, help="frame width in pixels for each stream")
    parser.add_argument("--height", type=int, default=480, help="frame height in pixels for each stream")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--quality", type=int, default=70, help="camera JPEG quality (1 to 100)")
    parser.add_argument("--depth", action="store_true", help="show RealSense depth with false colors beside RGB")
    parser.add_argument("--serial", help="select a RealSense camera by serial number")
    parser.add_argument("--retry-camera", action="store_true",
                        help="retry failed capture every 5 seconds (used by the boot service)")
    parser.add_argument("--device", help="V4L2 RGB device path (required for --source v4l2)")
    parser.add_argument("--control-mode", choices=("disabled", "simulation", "readonly", "hardware"), default="simulation")
    parser.add_argument("--control-token-file", help="private token file on this host (never a token value)")
    parser.add_argument("--dog-host", default="192.168.1.120", help="motion host IPv4 for UDP port 43893")
    parser.add_argument("--arm-device", default="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    parser.add_argument("--arm-host", help="optional SSH host for the read-only serial gateway")
    options = parser.parse_args(argv)
    if options.control_mode in ("hardware", "readonly") and not options.control_token_file:
        parser.error("Hardware and read-only modes require --control-token-file")
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
    try:
        if __package__:
            from .control import ControlService
            from .arm_gateway import ArmGateway
        else:
            from control import ControlService
            from arm_gateway import ArmGateway
        token = load_control_token(options.control_token_file)
        control = ControlService(mode=options.control_mode, dog_host=options.dog_host)
        arm_gateway = (ArmGateway(device=options.arm_device, host=options.arm_host)
                       if options.control_mode in ("hardware", "readonly") else None)
    except (OSError, ValueError) as error:
        print("Invalid development server configuration: {}".format(error), file=sys.stderr)
        return 2
    controller = CaptureController(options) if options.source != "none" else None
    frames = controller.frames if controller else LatestFrame("none")
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, request_stop)

    try:
        server = CameraHTTPServer(options.port, frames, controller, bind=options.bind,
                                  control=control, control_token=token, arm_gateway=arm_gateway)
    except OSError as error:
        print("Cannot listen on {}:{}: {}".format(options.bind, options.port, error), file=sys.stderr)
        return 1

    stopped = True
    with server:
        control.start()
        if controller:
            controller.start()
        try:
            print("http://{}:{}/".format(options.bind, server.server_port), flush=True)
            while not stopping:
                server.handle_request()
        finally:
            control.close()
            if arm_gateway:
                arm_gateway.close()
            stopped = controller.shutdown() if controller else True
    if not stopped:
        print("Camera shutdown timed out", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
