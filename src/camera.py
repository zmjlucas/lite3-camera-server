"""Camera capture, shared frames, and source switching."""
import argparse
import contextlib
import hashlib
import os
from pathlib import Path
import sys
import threading
import time

if __package__:
    from . import wide_angle
    from .camera_sources import discover_sources
else:
    import wide_angle
    from camera_sources import discover_sources


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
            if options.source == "rtsp":
                source = wide_angle.capture_frames(options.wide_angle_host, options.quality, stop,
                                                   lambda message: _report_capture_error(frames, message))
            elif options.source == "v4l2":
                source = v4l2_frames(options, stop)
            else:
                source = realsense_frames(options, frames, stop)
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
                           "backend": self._backend(entry), "available": entry["available"],
                           "error": entry["error"]})
        source_id = self.frames.snapshot()[0]["source_id"]
        selected = source_id if any(entry["id"] == source_id for entry in detected) else None
        return {"sources": public, "selected": selected}

    def _backend(self, entry):
        if entry["kind"] == "rtsp":
            return "rtsp"
        return "realsense" if entry["kind"] == "d435i" and self.options.source == "realsense" else "v4l2"

    def _source_options(self, entry):
        options = argparse.Namespace(**vars(self.options))
        options.source = self._backend(entry)
        if options.source == "realsense":
            options.device = None
            options.usb_path = entry["usb_path"]
        else:
            options.depth = False
            options.serial = None
            if options.source == "v4l2":
                options.device = entry["device"]
                options.pixel_format = entry["pixel_format"]
            else:
                options.device = None
        return options

    def _switchable_sources(self):
        detected = self.discover()
        host = self.options.wide_angle_host
        if not host:
            return detected
        identity = hashlib.sha256(wide_angle.endpoint(host).encode("ascii")).hexdigest()[:24]
        error = None
        try:
            wide_angle.probe(host)
        except RuntimeError as failure:
            error = str(failure)
        return [*detected, {"id": "camera-" + identity, "kind": "rtsp", "label": wide_angle.LABEL,
                           "device": host, "available": error is None, "error": error}]

    def _launch(self, options, selected, detected):
        options = argparse.Namespace(**vars(options))
        options.camera_sources = detected
        self.frames = LatestFrame(options.source, source_id=selected)
        self._stop = threading.Event()
        # The RTSP owner must finish reaping its child even if shutdown times out.
        producer = threading.Thread(target=self.capture_fn,
                                    args=(options, self.frames, self._stop),
                                    daemon=options.source != "rtsp")
        producer.start()
        self.producer = producer

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
            return self._catalog(self._switchable_sources())

    def select(self, source_id):
        with self._lock:
            if self._closed:
                raise RuntimeError("The camera server is stopping")
            detected = self._switchable_sources()
            entry = next((item for item in detected if item["id"] == source_id), None)
            if entry is None:
                raise ValueError("That camera is unavailable. Refresh the source list.")
            if not entry["available"]:
                raise RuntimeError(entry["error"] or "That camera is unavailable. Refresh the source list.")
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
