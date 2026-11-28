"""Preflight and supervised JPEG capture for the Lite3 vendor RTSP stream."""
import ipaddress
import os
from pathlib import Path
import re
import selectors
import socket
import struct
import subprocess
import sys
import time


LABEL = "Lite3 wide-angle camera"
RTSP_PORT = 8554
PROBE_TIMEOUT = 0.75
MAX_RTSP_HEADER = 8 * 1024
MAX_SDP = 8 * 1024
MAX_JPEG = 8 * 1024 * 1024
FIRST_FRAME_TIMEOUT = 4.0
NEXT_FRAME_TIMEOUT = 2.0
STOP_POLL_INTERVAL = 0.1
REAP_TIMEOUT = 0.3
OPEN_TIMEOUT_MS = 2500
READ_TIMEOUT_MS = 1000
MISSING_OPENCV = 2
OPEN_FAILED = 3
READ_FAILED = 4
ENCODE_FAILED = 5
WORKER_ERRORS = {
    0: "the RTSP decoder ended",
    MISSING_OPENCV: "OpenCV with FFmpeg and capture timeout support is required",
    OPEN_FAILED: "cannot open the vendor RTSP stream",
    READ_FAILED: "cannot read a frame from the vendor RTSP stream",
    ENCODE_FAILED: "cannot encode the wide-angle frame as JPEG",
}


def validate_host(value):
    """Accept the canonical motion-host IPv4 literal shared by the two CLIs."""
    message = "--wide-angle-host must be a canonical unicast IPv4 address"
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as error:
        raise ValueError(message) from error
    if (str(address) != value or address.is_unspecified or address.is_multicast
            or value == "255.255.255.255"):
        raise ValueError(message)
    return value


def endpoint(host):
    return "rtsp://{}:{}/test".format(validate_host(host), RTSP_PORT)


def _socket_deadline(connection, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise socket.timeout()
    connection.settimeout(remaining)


def _describe_reply(connection, deadline):
    header = bytearray()
    while b"\r\n\r\n" not in header:
        room = MAX_RTSP_HEADER - len(header)
        if not room:
            raise RuntimeError("RTSP response headers are too large")
        _socket_deadline(connection, deadline)
        chunk = connection.recv(room)
        if not chunk:
            raise RuntimeError("incomplete RTSP response headers")
        header.extend(chunk)

    headers, body = header.split(b"\r\n\r\n", 1)
    status, *fields = headers.split(b"\r\n")
    match = re.fullmatch(rb"RTSP/1\.0 ([0-9]{3})(?: [^\r\n]*)?", status)
    if not match:
        raise RuntimeError("invalid RTSP status line")
    if match[1] != b"200":
        raise RuntimeError("RTSP DESCRIBE returned {}".format(match[1].decode("ascii")))

    values = {}
    for field in fields:
        name, separator, value = field.partition(b":")
        if not separator or not re.fullmatch(rb"[A-Za-z0-9_-]+", name):
            raise RuntimeError("malformed RTSP response headers")
        values.setdefault(bytes(name).lower(), []).append(bytes(value).strip())
    sequences = values.get(b"cseq", [])
    if len(sequences) != 1 or sequences[0].lstrip(b"0") != b"1":
        raise RuntimeError("invalid RTSP CSeq")
    lengths = values.get(b"content-length", [])
    if len(lengths) != 1 or not lengths[0].isdigit():
        raise RuntimeError("invalid RTSP SDP length")
    digits = lengths[0].lstrip(b"0")
    if not 1 <= len(digits) <= len(str(MAX_SDP)) or int(digits) > MAX_SDP:
        raise RuntimeError("invalid RTSP SDP length")
    size = int(digits)

    while len(body) < size:
        _socket_deadline(connection, deadline)
        chunk = connection.recv(size - len(body))
        if not chunk:
            raise RuntimeError("incomplete RTSP SDP body")
        body.extend(chunk)
    if not any(re.match(rb"m=video[ \t]+\S", line) for line in body[:size].splitlines()):
        raise RuntimeError("the RTSP SDP advertises no video")


def probe(host):
    """Check the advertised video with one deadline and no playback session."""
    request = "DESCRIBE {} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n".format(endpoint(host))
    deadline = time.monotonic() + PROBE_TIMEOUT
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            _socket_deadline(connection, deadline)
            connection.connect((host, RTSP_PORT))
            _socket_deadline(connection, deadline)
            connection.sendall(request.encode("ascii"))
            _describe_reply(connection, deadline)
    except (OSError, RuntimeError) as error:
        reason = "RTSP DESCRIBE timed out" if isinstance(error, socket.timeout) else str(error)
        raise RuntimeError(
            "{}: {}. Check the motion host and vendor stream, then Refresh.".format(LABEL, reason)
        ) from error


def _worker_exit(process):
    try:
        code = process.wait(timeout=STOP_POLL_INTERVAL)
    except subprocess.TimeoutExpired:
        return "the RTSP decoder closed its output unexpectedly"
    return WORKER_ERRORS.get(code, "the RTSP decoder ended with status {}".format(code))


def _read_exact(process, selector, size, deadline, stop, phase):
    data = bytearray()
    while len(data) < size and not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("timed out waiting for the {} JPEG frame".format(phase))
        if not selector.select(min(remaining, STOP_POLL_INTERVAL)) or stop.is_set():
            continue
        try:
            chunk = os.read(process.stdout.fileno(), min(65536, size - len(data)))
        except BlockingIOError:
            continue
        if not chunk:
            raise RuntimeError(_worker_exit(process))
        data.extend(chunk)
    return data if not stop.is_set() else None


def _reap(process):
    try:
        process.stdout.close()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=REAP_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=REAP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    # Keep capture owned by this thread until the OS releases the child.
                    process.wait()


def capture_frames(host, quality, stop, report_error):
    """Yield framed JPEGs and own the decoder process until it has exited."""
    if stop.is_set():
        return
    process = None
    try:
        if os.name != "posix":
            raise RuntimeError("the RTSP decoder requires a POSIX host with selectable subprocess pipes")
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), validate_host(host), str(quality)],
            stdout=subprocess.PIPE, bufsize=0,
        )
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            phase, frame_timeout = "first", FIRST_FRAME_TIMEOUT
            while not stop.is_set():
                deadline = time.monotonic() + frame_timeout
                header = _read_exact(process, selector, 4, deadline, stop, phase)
                if header is None:
                    return
                size = struct.unpack(">I", header)[0]
                if not 0 < size <= MAX_JPEG:
                    raise RuntimeError("the RTSP decoder emitted an invalid JPEG length")
                jpeg = _read_exact(process, selector, size, deadline, stop, phase)
                if jpeg is None:
                    return
                yield bytes(jpeg)
                phase, frame_timeout = "next", NEXT_FRAME_TIMEOUT
    except (OSError, RuntimeError, ValueError) as error:
        if not stop.is_set():
            report_error("{}: {}".format(LABEL, error))
    finally:
        if process is not None:
            _reap(process)


def _decode(host, quality):
    os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
    try:
        import cv2

        backend = cv2.CAP_FFMPEG
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
                  cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS]
        if not cv2.videoio_registry.hasBackend(backend):
            print("The OpenCV FFmpeg backend is unavailable", file=sys.stderr)
            return MISSING_OPENCV
    except (ImportError, AttributeError) as error:
        print("Cannot load OpenCV FFmpeg support: {}".format(error), file=sys.stderr)
        return MISSING_OPENCV

    camera = cv2.VideoCapture()
    try:
        try:
            if not camera.open(endpoint(host), backend, params):
                return OPEN_FAILED
        except cv2.error as error:
            print("Cannot open the vendor RTSP stream: {}".format(error), file=sys.stderr)
            return OPEN_FAILED
        while True:
            try:
                ready, image = camera.read()
            except cv2.error as error:
                print("Cannot read the vendor RTSP stream: {}".format(error), file=sys.stderr)
                return READ_FAILED
            if not ready or image is None:
                return READ_FAILED
            try:
                encoded, buffer = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
            except cv2.error as error:
                print("Cannot encode the wide-angle frame: {}".format(error), file=sys.stderr)
                return ENCODE_FAILED
            if not encoded or not 0 < buffer.nbytes <= MAX_JPEG:
                return ENCODE_FAILED
            output = sys.stdout.buffer
            output.write(struct.pack(">I", buffer.nbytes))
            output.write(buffer)
            output.flush()
    except BrokenPipeError:
        return 0
    finally:
        camera.release()


if __name__ == "__main__":
    raise SystemExit(_decode(validate_host(sys.argv[1]), int(sys.argv[2])))
