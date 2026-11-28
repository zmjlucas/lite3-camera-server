"""Local, one-use R5 commissioning and bounded single-flight diagnostics."""
import copy
import hashlib
import json
import math
import os
import secrets
import stat
import threading
import time

try:
    from .arm_gateway import ArmGatewayError
    from .remote.arm_worker import LIMITS_DEG, SerialSession, decode_json, finite_angle, validate_device
except ImportError:  # Direct src/server.py deployment.
    from arm_gateway import ArmGatewayError
    from remote.arm_worker import LIMITS_DEG, SerialSession, decode_json, finite_angle, validate_device


TARGET_LIMITS = dict(LIMITS_DEG, h=45.0)
MAX_REFERENCE_OFFSET = {"b": 1.0, "s": 1.0, "e": 5.0, "w": 1.0}
DRIFT = {"b": 0.5, "s": 0.75, "e": 0.5, "w": 0.5}
SAMPLE_PERIOD = 0.1
MAX_SAMPLE_AGE = 0.25


class _Fault(Exception):
    pass


class _Cancelled(Exception):
    pass


def _identity():
    with open("/etc/machine-id", encoding="ascii") as machine:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as boot:
            return machine.read().strip(), boot.read().strip()


def _angles(value, limits):
    if not isinstance(value, dict) or set(value) != set(limits):
        raise ValueError("Invalid arm joint fields.")
    return {key: finite_angle(value[key], limit) for key, limit in limits.items()}


def _trusted(st):
    return st.st_uid in (0, os.geteuid()) and not st.st_mode & 0o022


class _Profile:
    """Pin the trusted directory while loading the record and claiming its nonce."""
    def __init__(self, path, device, identity_reader):
        path = os.path.abspath(path)
        self.directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not _trusted(os.fstat(self.directory)):
                raise ValueError("Unsafe arm commissioning directory.")
            fd = os.open(os.path.basename(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=self.directory)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or not _trusted(info):
                    raise ValueError("Unsafe arm commissioning record.")
                data = stream.read(16385)
            if len(data) > 16384:
                raise ValueError("Arm commissioning record exceeds 16 KiB.")
            record = decode_json(data)
            if (not isinstance(record, dict) or set(record) != {
                    "version", "machine_id", "boot_id", "device", "nonce", "targets_deg",
                    "observed_deg", "gripper_reference"} or
                    type(record["version"]) is not int or record["version"] != 1):
                raise ValueError("Invalid arm commissioning record.")
            for key in ("machine_id", "boot_id", "nonce", "gripper_reference"):
                if not isinstance(record[key], str) or not record[key].strip():
                    raise ValueError("Incomplete arm commissioning record.")
            if (record["device"] != device or
                    (record["machine_id"], record["boot_id"]) != identity_reader()):
                raise ValueError("Arm commissioning identity mismatch.")
            self.targets = _angles(record["targets_deg"], TARGET_LIMITS)
            self.observed = _angles(record["observed_deg"], LIMITS_DEG)
            # A full T122 resubmits even the non-moving joints' targets.
            for joint, limit in MAX_REFERENCE_OFFSET.items():
                if abs(self.targets[joint] - self.observed[joint]) > limit:
                    raise ValueError("Arm target and observed references exceed the allowed offset for " + joint + ".")
            digest = hashlib.sha256(record["nonce"].encode("utf-8")).hexdigest()
            self.marker = ".r5-used-" + digest
            self.claimed = False
            try:
                os.stat(self.marker, dir_fd=self.directory, follow_symlinks=False)
            except FileNotFoundError:
                self.used = False
            else:
                self.used = True
        except BaseException:
            self.close()
            raise

    def claim(self):
        if self.claimed:
            return
        try:
            fd = os.open(self.marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=self.directory)
            os.close(fd)
            os.fsync(self.directory)
        except OSError:
            raise _Fault("Arm commissioning record already used or could not be claimed.") from None
        self.claimed = True

    def close(self):
        if self.directory is not None:
            os.close(self.directory)
            self.directory = None


def _snapshot(op):
    return copy.deepcopy(op.state) if op is not None else None


def _stationary(samples):
    return len(samples) >= 3 and all(
        max(row[j] for row in samples) - min(row[j] for row in samples) <=
        (0.12 if j == "s" else 0.05) for j in LIMITS_DEG)


class _Operation:
    def __init__(self, joint, delta, speed, deadline, lease_deadline, targets):
        self.joint = joint
        self.delta = delta
        self.speed = speed
        self.start_target = targets[joint]
        self.goal = self.start_target + delta
        self.sign = 1 if delta > 0 else -1
        self.deadline = deadline
        self.lease_deadline = lease_deadline
        self.stop_deadline = None
        self.cancel = threading.Event()
        self.start_position = None
        self.first_write_position = None
        self.peak = 0.0
        self.sample = None
        self.sample_after_write = False
        self.state = {"operation_id": secrets.token_hex(12), "phase": "preflight", "reason": None,
                      "joint": joint, "delta_deg": delta, "speed_deg_s": speed,
                      "observed_start": None, "observed_end": None, "targets_written": [],
                      "hold_status": "not_needed", "observed_stationary": None,
                      "stop_verified": False, "gripper_position_deg": None}


class ArmMotionGateway:
    """The gate admits work and arbitrates one nonblocking write with revocation.

    The active worker, or one explicitly admitted idle read/reconnect, owns all
    serial I/O. Neither feedback, profile claim nor worker join holds the gate.
    """
    def __init__(self, device, profile_path, clock=time.monotonic, session_factory=None,
                 identity_reader=_identity):
        self.device = validate_device(device)
        self._clock = clock
        self._factory = session_factory
        if profile_path and os.name != "posix":
            raise ValueError("Arm commissioning requires a local POSIX serial descriptor.")
        self._profile = _Profile(profile_path, device, identity_reader) if profile_path else None
        self._targets = self._profile.targets.copy() if self._profile else None
        self._reason = None if self._profile else "Arm commissioning record required."
        if self._profile and self._profile.used:
            self._reason = "Arm commissioning record already used."
        self._gate = threading.Condition()
        self._closed = False
        self._busy = False
        self._read_failed = False
        self._connected = False
        self._session = None
        self._active = self._last = None

    def status(self):
        with self._gate:
            return {"motion_enabled": not self._closed and self._reason is None,
                    "reason": self._reason, "connected": self._connected,
                    "reconnect_required": self._read_failed,
                    "active_operation": _snapshot(self._active), "last_operation": _snapshot(self._last)}

    def operation(self, operation_id):
        with self._gate:
            op = self._active or self._last
            if op is None or op.state["operation_id"] != operation_id:
                raise KeyError("Unknown arm operation.")
            return _snapshot(op)

    def _available(self):
        if self._closed:
            raise ArmGatewayError("Arm gateway is closed.")
        if self._busy or self._active:
            raise ArmGatewayError("Arm gateway is busy.")

    def _read(self):
        if self._session is None:
            if self._factory is None:
                self._session = SerialSession(self.device, timeout=0.20, strict_feedback=True)
            else:
                self._session = self._factory(self.device)
                self._session.strict_feedback = True
        started = self._clock()
        state = self._session.read_state()
        if self._clock() - started > MAX_SAMPLE_AGE:
            raise _Fault("Stale arm feedback.")
        if (state.get("observed") is not True or "gripper_position_deg" not in state or
                state["gripper_position_deg"] is not None):
            raise _Fault("Invalid arm feedback.")
        position = _angles(state["position_deg"], LIMITS_DEG)
        with self._gate:
            self._connected = True
        return state, {"position_deg": position, "sample_monotonic": started}

    def _disconnect(self):
        if self._session is not None:
            self._session.close()
            self._session = None
        with self._gate:
            self._connected = False

    def _schedule_close(self):
        # Called under the gate. The last owner arranges shutdown. Device close
        # can block in a driver, so neither admission nor shutdown joins it indefinitely.
        if not self._closed or self._active or self._busy:
            return
        if self._profile is not None:
            # The profile pins a local directory descriptor. No serial driver is involved.
            self._profile.close()
            self._profile = None
        session = self._session
        if session is None:
            return

        def retire():
            try:
                session.close()
            finally:
                self._release_idle()

        # The worker's final gate acquisition follows this ownership transfer.
        # If it cannot start, a later close still owns the session.
        threading.Thread(target=retire, name="r5-close", daemon=True).start()
        self._busy = True
        self._session = None
        self._connected = False

    def _release_idle(self):
        with self._gate:
            self._busy = False
            self._schedule_close()
            self._gate.notify_all()

    def read_state(self):
        with self._gate:
            self._available()
            if self._read_failed:
                raise ArmGatewayError("Arm read failed; explicit reconnect required.")
            self._busy = True
        try:
            if self._profile and self._reason is None:
                self._profile.claim()
            state, sample = self._read()
            with self._gate:
                if self._reason is None:
                    self._reason = self._reference_error(sample["position_deg"])
                return dict(state, motion_enabled=self._reason is None and not self._closed,
                            reason=self._reason, sample_monotonic=sample["sample_monotonic"])
        except Exception:
            with self._gate:
                self._reason = "Arm read failed; fresh commissioning required."
                self._read_failed = True
            self._disconnect()
            raise ArmGatewayError("Arm read failed; explicit reconnect required.") from None
        finally:
            self._release_idle()

    def reconnect(self):
        with self._gate:
            self._available()
            self._busy = True
            self._reason = "Arm reconnect requires fresh commissioning."
        try:
            if self._profile:
                try:
                    self._profile.claim()
                except _Fault:
                    pass  # Already consumed or unavailable: observation remains available.
            self._disconnect()
            with self._gate:
                self._read_failed = False
                return {"connected": False, "reconnect_required": False,
                        "motion_enabled": False, "reason": self._reason}
        finally:
            self._release_idle()

    def jog(self, joint, delta, speed, lease_deadline):
        if not isinstance(joint, str) or joint not in ("b", "e", "h"):
            raise ValueError("Supported arm jog joints are b, e and h.")
        delta = finite_angle(delta, 2.0 if joint == "e" else 0.5)
        speed = finite_angle(speed, 1.0)
        if abs(delta) < 0.1 or speed <= 0 or abs(delta) > 5.0 * speed:
            raise ValueError("Invalid arm jog size, speed or duration.")
        with self._gate:
            self._available()
            if self._reason:
                raise ArmGatewayError(self._reason)
            now = self._clock()
            if not self._live_deadline(lease_deadline, now):
                raise ArmGatewayError("Arm lease expired.")
            goal = self._targets[joint] + delta
            self._validate_target(joint, goal)
            op = _Operation(joint, delta, speed, now + 3.0 + abs(delta) / speed,
                            lease_deadline, self._targets)
            worker = threading.Thread(target=self._run, args=(op,), name="r5-diagnostic", daemon=True)
            self._active = op
            try:
                worker.start()
            except BaseException:
                self._active = None
                raise
            return _snapshot(op)

    @staticmethod
    def _live_deadline(deadline, now):
        return type(deadline) in (int, float) and math.isfinite(deadline) and deadline > now

    def _validate_target(self, joint, value):
        finite_angle(value, TARGET_LIMITS[joint])
        travel = value - self._profile.targets[joint]
        if joint in LIMITS_DEG:
            finite_angle(self._profile.observed[joint] + travel, LIMITS_DEG[joint])
        if abs(travel) > 3.0:
            raise ValueError("Arm target exceeds commissioning envelope.")

    def _cancel(self, op, reason):
        if not op.cancel.is_set():
            op.state.update(phase="stopping", reason=reason)
            if op.state["targets_written"] and op.joint != "h":
                op.state["hold_status"] = "requested"
            op.stop_deadline = min(self._clock(), op.lease_deadline) + 0.5
            op.cancel.set()

    def renew(self, lease_deadline):
        with self._gate:
            op = self._active
            if op and not op.cancel.is_set():
                now = self._clock()
                if now >= op.lease_deadline or not self._live_deadline(lease_deadline, now):
                    self._cancel(op, "lease_expired")
                else:
                    op.lease_deadline = lease_deadline

    def stop(self, reason="stop"):
        with self._gate:
            op = self._active
            if op:
                self._cancel(op, reason)
            return _snapshot(op)

    def close(self):
        with self._gate:
            self._closed = True
            self._reason = "Arm gateway is closed."
            if self._active:
                self._cancel(self._active, "shutdown")
            self._schedule_close()
            self._gate.wait_for(lambda: not self._active and not self._busy, timeout=0.8)

    def _check(self, op):
        now = self._clock()
        if now >= op.lease_deadline:
            self._cancel(op, "lease_expired")
        if op.cancel.is_set():
            raise _Cancelled()
        if now >= op.deadline:
            raise _Fault("Arm operation timed out.")

    def _reference_error(self, pose, moving_axis=None):
        profile = self._profile
        for axis, value in pose.items():
            if abs(value - profile.observed[axis]) > 3.0:
                return "Observed arm joint {} exceeds its commissioning envelope.".format(axis)
            expected = profile.observed[axis] + (self._targets[axis] - profile.targets[axis])
            if axis != moving_axis and abs(value - expected) > DRIFT[axis]:
                return "Observed arm joint {} differs from its expected target reference.".format(axis)
        return None

    def _sample(self, op):
        _, sample = self._read()
        pose = sample["position_deg"]
        moving_axis = op.joint if op.start_position is not None else None
        error = self._reference_error(pose, moving_axis)
        if error:
            raise _Fault(error)
        if op.start_position is not None:
            for axis, value in pose.items():
                if axis != op.joint and abs(value - op.start_position[axis]) > DRIFT[axis]:
                    raise _Fault("Unexpected non-target arm joint movement.")
        if op.start_position is not None and op.joint != "h":
            displacement = op.sign * (pose[op.joint] - op.start_position[op.joint])
            if displacement < op.peak - 0.2:
                raise _Fault("Arm joint moved in the wrong direction.")
            op.peak = max(op.peak, displacement)
            mapped = self._mapped(op, pose)
            if op.sign * (mapped - self._targets[op.joint]) > 0.5:
                raise _Fault("Arm joint moved beyond its submitted target.")
        op.sample = sample
        op.sample_after_write = True
        with self._gate:
            op.state["observed_end"] = sample
        return pose

    def _mapped(self, op, pose):
        j = op.joint
        return self._profile.targets[j] + (pose[j] - self._profile.observed[j])

    def _write(self, op, moving, hold=False):
        joint = op.joint
        self._validate_target(joint, moving)
        targets = dict(self._targets)
        targets[joint] = moving
        command = dict(targets, T=122, spd=op.speed, acc=1)
        payload = (json.dumps(command, allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")
        port_fd = self._session._port.fileno()
        if os.get_blocking(port_fd):
            raise _Fault("Arm serial descriptor must support nonblocking writes.")
        with self._gate:
            now = self._clock()
            if hold:
                # Expiry/shutdown revoke all advances. The one retraction has
                # its own deadline and requires the original exclusive session.
                if (self._active is not op or not op.cancel.is_set() or
                        op.state["hold_status"] != "requested" or now >= op.stop_deadline):
                    raise _Fault("Arm hold deadline unavailable.")
            else:
                self._check(op)
            if now - op.sample["sample_monotonic"] > MAX_SAMPLE_AGE:
                raise _Fault("Stale arm feedback before target write.")
            # One syscall only; an exception/short count makes command history ambiguous.
            try:
                count = os.write(port_fd, payload)
            except OSError:
                count = -1
            if count != len(payload):
                op.state["hold_status"] = "unavailable"
                raise _Fault("Ambiguous arm target write.")
            if not op.state["targets_written"] and joint != "h":
                op.first_write_position = op.sample["position_deg"][joint]
            self._targets = targets
            op.state["targets_written"].append(targets)
            op.sample_after_write = False
            if hold:
                op.state["hold_status"] = "written"

    def _preflight(self, op):
        poses = []
        for i in range(5):
            if i:
                op.cancel.wait(SAMPLE_PERIOD)
            with self._gate:
                self._check(op)
            poses.append(self._sample(op))
        for axis in LIMITS_DEG:
            values = [pose[axis] for pose in poses]
            if max(values) - min(values) > (0.6 if axis == "s" else 0.3):
                raise _Fault("Arm preflight readings are unstable.")
        op.start_position = poses[-1]
        with self._gate:
            self._check(op)
            op.state["observed_start"] = op.sample
            op.state["phase"] = "moving"

    def _monitor_gripper(self, op):
        self._write(op, op.goal)
        until = self._clock() + abs(op.delta) / op.speed + 0.2
        window = []
        while True:
            op.cancel.wait(SAMPLE_PERIOD)
            with self._gate:
                self._check(op)
            window.append(self._sample(op))
            if self._clock() >= until and len(window) >= 3:
                if not _stationary(window[-3:]):
                    with self._gate:
                        op.state["observed_stationary"] = False
                    raise _Fault("Observed arm joints are moving after the gripper command.")
                return "commanded_unverified", True

    def _move(self, op):
        last_write = self._clock()
        last_progress = last_write
        progress = 0.0
        stable = []
        j = op.joint
        tol = min(0.15, abs(op.delta) / 2)
        while True:
            op.cancel.wait(SAMPLE_PERIOD)
            with self._gate:
                self._check(op)
            pose = self._sample(op)
            mapped = self._mapped(op, pose)
            now = self._clock()
            displacement = (op.sign * (pose[j] - op.first_write_position)
                            if op.first_write_position is not None else 0.0)
            if displacement >= progress + min(0.02, op.speed * 0.3):
                progress, last_progress = displacement, now
            if op.state["targets_written"] and now - last_progress >= 1.5:
                raise _Fault("No credible arm joint progress.")
            previous = self._targets[j]
            if previous == op.goal:
                minimum_travel = abs(op.delta) / 2 - 1e-9
                if (abs(op.goal - mapped) <= tol and displacement >= minimum_travel and
                        op.sign * (pose[j] - op.start_position[j]) >= minimum_travel):
                    stable.append(pose)
                    stable = stable[-3:]
                    if _stationary(stable):
                        return "completed", True
                else:
                    stable = []
                continue
            distance = min(0.25, op.speed * 0.5, abs(op.goal - previous))
            if now - last_write < distance / op.speed:
                continue
            candidate = op.goal if distance == abs(op.goal - previous) else previous + op.sign * distance
            lead = op.sign * (candidate - mapped)
            if lead < -0.05:
                raise _Fault("Requested arm step is behind the measured joint.")
            if lead <= 0.5:
                if not op.state["targets_written"]:
                    last_progress = now
                self._write(op, candidate)
                last_write = self._clock()

    def _hold(self, op):
        if op.joint == "h" or not op.state["targets_written"]:
            return
        if self._clock() >= op.stop_deadline:
            raise _Fault("Arm hold window expired.")
        if not op.sample_after_write:
            self._sample(op)
        mapped = self._mapped(op, op.sample["position_deg"])
        low, high = sorted((op.start_target, self._targets[op.joint]))
        if not low - 0.2 <= mapped <= high + 0.2:
            raise _Fault("Arm feedback outside the commanded segment.")
        moving = min(high, max(low, mapped))
        if abs(moving - self._targets[op.joint]) > 0.5:
            raise _Fault("Arm hold exceeds the permitted feedback lead.")
        self._write(op, moving, hold=True)
        samples = []
        while len(samples) < 3 and self._clock() < op.stop_deadline:
            samples.append(self._sample(op))
            if len(samples) < 3:
                time.sleep(SAMPLE_PERIOD)
        if len(samples) < 3:
            raise _Fault("Arm hold observation window expired.")
        stationary = _stationary(samples)
        with self._gate:
            op.state["observed_stationary"] = stationary
        if not stationary:
            raise _Fault("Arm joint readings after hold are unstable.")

    def _run(self, op):
        phase, stationary, fault = None, None, None
        try:
            try:
                self._profile.claim()
                self._preflight(op)
                if op.joint == "h":
                    phase, stationary = self._monitor_gripper(op)
                else:
                    phase, stationary = self._move(op)
                with self._gate:
                    self._check(op)
                    self._finish(op, phase, stationary, None)
                    return
            except _Cancelled:
                self._hold(op)
                phase = "cancelled"
        except Exception as exc:
            fault = str(exc) if isinstance(exc, _Fault) else "Arm feedback or transport failed."
            self._disconnect()
            phase = "fault"
        with self._gate:
            self._finish(op, phase, stationary, fault)

    def _finish(self, op, phase, stationary, fault):
        if fault:
            self._reason = fault
            self._read_failed = True
            if op.state["targets_written"] and op.state["hold_status"] != "written":
                op.state["hold_status"] = "unavailable"
        if stationary is not None:
            op.state["observed_stationary"] = stationary
        op.state.update(phase=phase, reason=fault or op.state["reason"])
        self._last, self._active = op, None
        self._schedule_close()
        self._gate.notify_all()
