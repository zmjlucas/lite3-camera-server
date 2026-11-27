#!/usr/bin/env python3
"""Fixed, read-only R5 serial worker; Python 3.6 and pySerial 3.4 compatible.

Only T105 feedback queries may leave this process. Hardware motion has no
implementation or override here.
"""
import argparse
import json
import math
import re
import sys
import time


MOTION_BLOCK_REASON = "Hardware arm motion is disabled."
LIMITS_DEG = {"b": 180.0, "s": 90.0, "e": 90.0, "w": 90.0}
MAX_LINE = 4096
MAX_QUERY_BYTES = 16384
MAX_REQUEST = 512


def validate_device(device):
    if (not isinstance(device, str) or len(device) > 255 or
            not re.fullmatch(r"/dev/[A-Za-z0-9_./-]+", device) or
            any(part in ("", ".", "..") for part in device.split("/")[2:])):
        raise ValueError("Arm device must be a plain absolute path under /dev.")
    return device


def finite_angle(value, limit):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or abs(value) > limit):
        raise ValueError("Invalid or out-of-range arm feedback.")
    return float(value)


def observed_state(position):
    return {
        "position_deg": position,
        "gripper_position_deg": None,
        "observed": True,
        "motion_enabled": False,
        "reason": MOTION_BLOCK_REASON,
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field.")
        result[key] = value
    return result


def decode_json(data):
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)


class SerialSession:
    """Own a single lazy serial open, latched closed on any query failure."""

    def __init__(self, device, timeout=0.8):
        self.device = validate_device(device)
        self.timeout = finite_angle(timeout, 10.0)
        if self.timeout <= 0:
            raise ValueError("Arm read timeout must be positive.")
        self._port = None
        self._failed = False
        self._closed = False

    def _open(self):
        # Importing this module and constructing the gateway need no pySerial.
        import serial
        self._port = serial.Serial(port=None, baudrate=115200, timeout=0.05,
                                   write_timeout=0.2, exclusive=True)
        # pySerial 3.4 __enter__ opens port=None prematurely. Do not use it.
        self._port.dtr = False
        self._port.rts = False
        self._port.port = self.device
        self._port.open()

    def read_state(self):
        if self._closed or self._failed:
            raise RuntimeError("Arm session unavailable; explicit reconnect required.")
        try:
            if self._port is None:
                self._open()
            return self._query()
        except Exception:
            self._failed = True
            self._close_port()
            raise

    def _query(self):
        # Reject buffered observations from earlier reads; never return a cache.
        self._port.reset_input_buffer()
        request = b'{"T":105}\n'
        if self._port.write(request) != len(request):
            raise OSError("Incomplete arm query write.")
        deadline = time.monotonic() + self.timeout
        line = bytearray()
        received = 0
        while time.monotonic() < deadline:
            self._port.timeout = min(0.05, max(0.001, deadline - time.monotonic()))
            chunk = self._port.read(1)
            if not chunk:
                continue
            line.extend(chunk)
            received += len(chunk)
            if len(line) > MAX_LINE or received > MAX_QUERY_BYTES:
                raise ValueError("Arm feedback exceeded output limit.")
            if chunk != b"\n":
                continue
            try:
                value = decode_json(bytes(line))
            except (ValueError, UnicodeError):
                value = None
            line.clear()
            if (not isinstance(value, dict) or type(value.get("T")) is not int or
                    value["T"] != 1051):
                continue
            position = {}
            for axis, limit in LIMITS_DEG.items():
                radians = finite_angle(value.get(axis), math.radians(limit))
                position[axis] = math.degrees(radians)
            if time.monotonic() >= deadline:
                raise TimeoutError("Arm feedback timed out.")
            return observed_state(position)
        raise TimeoutError("Arm feedback timed out.")

    def _close_port(self):
        port, self._port = self._port, None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass

    def close(self):
        self._closed = True
        self._close_port()


def serve(device, incoming, outgoing):
    session = SerialSession(device)
    try:
        while True:
            line = incoming.readline(MAX_REQUEST + 1)
            if not line:
                return
            request_id = None
            oversized = len(line) > MAX_REQUEST or not line.endswith(b"\n")
            try:
                if oversized:
                    raise ValueError("Request exceeds limit.")
                request = decode_json(line)
                if (not isinstance(request, dict) or
                        set(request) != {"id", "action"} or
                        type(request["id"]) is not int or
                        not 0 < request["id"] < 2 ** 53 or
                        request["action"] != "read_state"):
                    raise ValueError("Only read_state requests are supported.")
                request_id = request["id"]
                reply = {"id": request_id, "ok": True, "state": session.read_state()}
            except Exception:
                # Do not forward serial/SSH exception text or environment details.
                reply = {"id": request_id, "ok": False,
                         "error": "Read unavailable or invalid request; explicit reconnect required."}
            outgoing.write((json.dumps(reply, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8"))
            outgoing.flush()
            if oversized:
                return
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(description="Read-only R5 feedback worker")
    parser.add_argument("--device", required=True, type=validate_device)
    args = parser.parse_args()
    serve(args.device, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    main()
