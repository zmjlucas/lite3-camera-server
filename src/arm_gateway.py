"""Bounded, serialized local/SSH access to the fixed read-only R5 worker."""
import math
import os
import re
import selectors
import shlex
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any, Literal, Optional
    from lite3_dev_server.api import (
        ArmCachedFeedback, ArmGatewayReconnect, ArmObservationStatus, ArmSample, ObservedArmState,
    )

if TYPE_CHECKING or not __package__:  # Direct src/server.py deployment.
    from remote.arm_worker import (LIMITS_DEG, MAX_LINE, MOTION_BLOCK_REASON,
                                  decode_json, finite_angle, observed_state,
                                  validate_device)
else:
    from .remote.arm_worker import (LIMITS_DEG, MAX_LINE, MOTION_BLOCK_REASON,
                                    decode_json, finite_angle, observed_state,
                                    validate_device)


class ArmGatewayError(RuntimeError):
    """An arm operation failed; no hardware movement is implemented."""


def cached_feedback(sample: 'Optional[ArmSample]', source: 'Literal["local_serial", "ssh_serial"]',
                    now: float, connected: bool) -> 'Optional[ArmCachedFeedback]':
    """Copy the last observation using the gateway host's query-start clock."""
    if sample is None:
        return None
    age = max(0.0, now - sample["sample_monotonic"])
    return {"position_deg": dict(sample["position_deg"]),
            "sample_monotonic": sample["sample_monotonic"], "source": source,
            "age_s": age, "fresh": connected and age <= 0.25,
            "observed": True, "gripper_position_deg": None}


def _validate_host(host):
    if host is None:
        return None
    if (not isinstance(host, str) or len(host) > 255 or
            not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*", host)):
        raise ValueError("Arm host must be an SSH hostname or a target such as ysc@192.168.1.103.")
    return host


class ArmGateway:
    """One lazy worker owner; failures require explicit reconnect, never retry.

    This lock is private to arm reads. Neither the dog watchdog nor stop handling
    should acquire it. A queued read shares its time budget with lock acquisition.
    """

    def __init__(self, device="/dev/ttyUSB0", host=None, timeout=3.0, clock=time.monotonic):
        self.device = validate_device(device)
        self.host = _validate_host(host)
        self.timeout = finite_angle(timeout, 30.0)
        if self.timeout <= 0:
            raise ValueError("Arm gateway timeout must be positive.")
        self._lock = threading.Lock()
        # Status never waits for the I/O lock or for a worker to exit.
        self._state_lock = threading.Lock()
        self._clock = clock
        self._sample: 'Optional[ArmSample]' = None
        self._connected = False
        self._process = None
        self._failed = False
        self._closed = False
        self._request_id = 0

    def status(self) -> 'ArmObservationStatus':
        with self._state_lock:
            connected = self._connected and not self._failed and not self._closed
            return {
                "connected": connected, "reconnect_required": self._failed,
                "commissioning": {"state": "closed" if self._closed else "readonly",
                                  "profile_loaded": False, "nonce_consumed": False},
                "cached_feedback": cached_feedback(
                    self._sample, "ssh_serial" if self.host else "local_serial", self._clock(), connected),
            }

    def _command(self):
        worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "remote", "arm_worker.py")
        if self.host is None:
            return [sys.executable, "-u", worker, "--device", self.device]
        with open(worker, "r", encoding="utf-8") as source:
            script = source.read()
        # SSH invokes a remote shell. Quote the bundled source and validated
        # device as complete arguments; no API input can provide executable code.
        command = "python3 -u -c {} --device {}".format(shlex.quote(script), shlex.quote(self.device))
        return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout={}".format(max(1, int(math.ceil(self.timeout)))),
                "-o", "ServerAliveInterval=1", "-o", "ServerAliveCountMax=2",
                "-o", "ControlMaster=no", "-o", "ControlPath=none", self.host, command]

    def _start(self):
        self._process = subprocess.Popen(self._command(), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         bufsize=0, close_fds=True)
        os.set_blocking(self._process.stdin.fileno(), False)
        os.set_blocking(self._process.stdout.fileno(), False)

    def _wait(self, stream, event, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ArmGatewayError("Arm read timed out; explicit reconnect required.")
        with selectors.DefaultSelector() as selector:
            selector.register(stream, event)
            if not selector.select(remaining):
                raise ArmGatewayError("Arm read timed out; explicit reconnect required.")

    def _exchange(self, deadline) -> 'ObservedArmState':
        self._request_id += 1
        request = ('{"id":%d,"action":"read_state"}\n' % self._request_id).encode("ascii")
        sent = 0
        while sent < len(request):
            self._wait(self._process.stdin, selectors.EVENT_WRITE, deadline)
            try:
                count = os.write(self._process.stdin.fileno(), request[sent:])
            except BlockingIOError:
                continue
            if count <= 0:
                raise ArmGatewayError("Arm worker closed; explicit reconnect required.")
            sent += count
        response = bytearray()
        while True:
            self._wait(self._process.stdout, selectors.EVENT_READ, deadline)
            try:
                chunk = os.read(self._process.stdout.fileno(), MAX_LINE + 1 - len(response))
            except BlockingIOError:
                continue
            if not chunk:
                raise ArmGatewayError("Arm worker closed; explicit reconnect required.")
            response.extend(chunk)
            if len(response) > MAX_LINE:
                raise ArmGatewayError("Arm response exceeded limit; explicit reconnect required.")
            if b"\n" in response:
                if response.count(b"\n") != 1 or not response.endswith(b"\n"):
                    raise ValueError("Unsolicited arm worker output.")
                value = decode_json(bytes(response))
                return self._validate_reply(value)

    def _validate_reply(self, reply: 'Any') -> 'ObservedArmState':
        if (not isinstance(reply, dict) or type(reply.get("id")) is not int or
                reply["id"] != self._request_id or reply.get("ok") is not True):
            raise ArmGatewayError("Arm feedback unavailable; explicit reconnect required.")
        state = reply.get("state")
        if (not isinstance(state, dict) or state.get("observed") is not True or
                state.get("motion_enabled") is not False or
                "gripper_position_deg" not in state or state["gripper_position_deg"] is not None):
            raise ValueError("Invalid arm observation.")
        position = state.get("position_deg")
        if not isinstance(position, dict) or set(position) != set(LIMITS_DEG):
            raise ValueError("Invalid arm position fields.")
        return observed_state({axis: finite_angle(position[axis], limit)
                               for axis, limit in LIMITS_DEG.items()})

    def read_state(self) -> 'ObservedArmState':
        if os.name == "nt":
            raise ArmGatewayError("Hardware arm gateway is unsupported on Windows; run it on a POSIX host.")
        deadline = time.monotonic() + self.timeout
        if not self._lock.acquire(timeout=self.timeout):
            raise ArmGatewayError("Arm gateway is busy; read timed out.")
        try:
            if self._closed:
                raise ArmGatewayError("Arm gateway is closed.")
            if self._failed:
                raise ArmGatewayError("Arm gateway failed; explicit reconnect required.")
            try:
                started = self._clock()
                if self._process is None:
                    self._start()
                state = self._exchange(deadline)
                with self._state_lock:
                    self._sample = {"position_deg": dict(state["position_deg"]), "sample_monotonic": started}
                    self._connected = True
                return state
            except Exception:
                with self._state_lock:
                    self._failed = True
                self._stop_process()
                raise ArmGatewayError("Arm read failed; explicit reconnect required.") from None
        finally:
            self._lock.release()

    def _stop_process(self):
        with self._state_lock:
            self._connected = False
        process, self._process = self._process, None
        if process is None:
            return
        try:
            # EOF also tells the remote helper to release its persistent owner.
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=0.2)
            except (subprocess.TimeoutExpired, TimeoutError):
                process.kill()
                process.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired, TimeoutError):
            pass
        finally:
            if process.stdout is not None:
                process.stdout.close()

    def reconnect(self) -> 'ArmGatewayReconnect':
        # Explicit reconnect resets the failure latch but remains lazy: the next
        # explicit read is the only action that may open serial or SSH.
        with self._lock:
            if self._closed:
                raise ArmGatewayError("Arm gateway is closed.")
            self._stop_process()
            with self._state_lock:
                self._failed = False
            return {"connected": False, "reconnect_required": False,
                    "motion_enabled": False, "reason": MOTION_BLOCK_REASON}

    def close(self):
        with self._lock:
            with self._state_lock:
                self._closed = True
            self._stop_process()
