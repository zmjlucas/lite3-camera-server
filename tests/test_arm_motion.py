"""Offline, byte-level R5 tests. Synthetic fixtures belong to these tests only."""
import json
import math
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.arm_gateway import ArmGatewayError
from src.arm_motion import ArmMotionGateway
from src.remote.arm_worker import SerialSession


class Controller:
    """Generate T1051 from the T122 bytes submitted to a real nonblocking pipe."""
    def __init__(self, targets, observed):
        self.targets = targets.copy()
        self.observed = observed.copy()
        self.offset = {j: observed[j] - targets[j] for j in observed}
        self.requests = []
        self.motions = []
        self.pending = bytearray()
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)
        self.respond = None

    def fileno(self):
        return self.write_fd

    def drain(self):
        try:
            data = os.read(self.read_fd, 16384)
        except BlockingIOError:
            return
        for line in data.splitlines():
            cmd = json.loads(line.decode("ascii"))
            self.motions.append(cmd)
            for axis in self.observed:
                self.observed[axis] = cmd[axis] + self.offset[axis]

    @property
    def in_waiting(self):
        return len(self.pending)

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, payload):
        assert payload == b'{"T":105}\n'
        self.requests.append(payload)
        self.drain()
        value = dict((k, math.radians(v)) for k, v in self.observed.items())
        value["T"] = 1051
        if self.respond:
            value = self.respond(self, value)
        if isinstance(value, dict):
            value = (json.dumps(value) + "\n").encode("ascii")
        self.pending.extend(value)
        return len(payload)

    def read(self, size):
        value = bytes(self.pending[:size])
        del self.pending[:size]
        if not value:
            time.sleep(0.001)
        return value

    def close(self):
        # The test owns these descriptors until teardown so it can inspect final bytes.
        pass

    def cleanup(self):
        os.close(self.write_fd)
        os.close(self.read_fd)


@unittest.skipUnless(os.name == "posix", "Local R5 commissioning uses a POSIX serial descriptor")
class MotionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "profile.json"
        self.profile = {"version": 1, "machine_id": "fixture-host", "boot_id": "fixture-boot",
                        "nonce": "fixture-one-use-id", "device": "/dev/testR5",
                        "targets_deg": {"b": 10, "s": -4, "e": 8, "w": 6, "h": -16},
                        "observed_deg": {"b": 10.1, "s": -4.2, "e": 11.69, "w": 6.05},
                        "gripper_reference": "Synthetic offline controller"}
        self.save()
        self.board = Controller(self.profile["targets_deg"], self.profile["observed_deg"])
        self.addCleanup(self.board.cleanup)
        self.opens = []

    def save(self):
        self.path.write_text(json.dumps(self.profile))
        self.path.chmod(0o600)

    def gateway(self, path=True, clock=time.monotonic):
        def factory(device):
            session = SerialSession(device, timeout=0.05)
            def open_port():
                self.opens.append(device)
                session._port = self.board
            session._open = open_port
            return session
        g = ArmMotionGateway("/dev/testR5", self.path if path is True else path,
                             clock=clock, session_factory=factory,
                             identity_reader=lambda: ("fixture-host", "fixture-boot"))
        self.addCleanup(g.close)
        return g

    def jog(self, g, joint="e", delta=0.25, speed=1):
        return g.jog(joint, delta, speed, time.monotonic() + 15)

    def finish(self, g, op, timeout=9):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = g.operation(op["operation_id"])
            if result["phase"] in ("completed", "fault", "cancelled", "commanded_unverified"):
                self.board.drain()
                return result
            time.sleep(0.01)
        self.fail("operation did not finish")

    def test_profile_security_and_identity_fail_before_serial(self):
        for key, bad in (("machine_id", "other"), ("boot_id", "other"),
                         ("version", True), ("device", "/dev/other"),
                         ("targets_deg", dict(self.profile["targets_deg"], h=True)),
                         ("observed_deg", dict(self.profile["observed_deg"], e=91)),
                         ("gripper_reference", " ")):
            with self.subTest(key=key):
                original = self.profile[key]
                self.profile[key] = bad
                self.save()
                with self.assertRaises((ValueError, OSError)):
                    self.gateway()
                self.profile[key] = original
        self.save()
        self.path.chmod(0o622)
        with self.assertRaises((ValueError, OSError)):
            self.gateway()
        self.save()
        link = self.path.with_suffix(".link")
        link.symlink_to(self.path)
        with self.assertRaises((ValueError, OSError)):
            self.gateway(link)
        self.path.write_bytes(b'{"version":1,' + self.path.read_bytes()[1:])
        with self.assertRaises(ValueError):
            self.gateway()
        self.path.write_bytes(b" " * 16385)
        with self.assertRaises(ValueError):
            self.gateway()
        self.assertFalse(self.opens)

    def test_elbow_stages_preserve_other_commanded_coordinates_and_offsets(self):
        g = self.gateway()
        result = self.finish(g, self.jog(g, delta=-.6))
        self.assertEqual(result["phase"], "completed", result)
        self.assertEqual(g.status()["commissioning"]["state"], "ready")
        self.assertEqual(g.status()["cached_feedback"]["position_deg"], result["observed_end"]["position_deg"])
        self.assertTrue(result["observed_stationary"])
        self.assertFalse(result["stop_verified"])
        self.assertIsNone(result["gripper_position_deg"])
        self.assertGreaterEqual(len(self.board.motions), 3)
        last = 8
        for cmd in self.board.motions:
            self.assertEqual(set(cmd), set("bsewh") | {"T", "spd", "acc"})
            self.assertEqual((cmd["b"], cmd["s"], cmd["w"], cmd["h"]), (10, -4, 6, -16))
            self.assertEqual((cmd["T"], cmd["spd"], cmd["acc"]), (122, 1, 1))
            self.assertLessEqual(abs(cmd["e"] - last), 0.25000001)
            last = cmd["e"]
        self.assertAlmostEqual(last, 7.4)
        self.assertAlmostEqual(result["observed_end"]["position_deg"]["e"], 11.09)
        self.assertEqual(result["targets_written"][-1]["e"], last)
        self.assertTrue(g.read_state()["motion_enabled"])
        g.close()
        try:
            restarted = self.gateway()
            op = self.jog(restarted)
        except (ValueError, ArmGatewayError):
            return
        self.assertEqual(self.finish(restarted, op)["phase"], "fault")

    def test_cancel_and_expiry_during_fifth_query_never_write(self):
        for why in ("stop", "expiry"):
            with self.subTest(why=why):
                self.profile["nonce"] = "race-" + why
                self.save()
                entered, release = threading.Event(), threading.Event()
                offset = [0]
                g = self.gateway(clock=lambda: time.monotonic() + offset[0])
                start = len(self.board.requests)
                def block(board, sample):
                    if len(board.requests) == start + 5:
                        entered.set()
                        release.wait(2)
                    return sample
                self.board.respond = block
                op = self.jog(g)
                self.assertTrue(entered.wait(2))
                try:
                    with self.assertRaises(ArmGatewayError):
                        self.jog(g)
                    with self.assertRaises(ArmGatewayError):
                        g.reconnect()
                    if why == "stop":
                        self.assertEqual(g.stop()["operation_id"], op["operation_id"])
                    else:
                        offset[0] += 16
                        g.renew(time.monotonic() + 30)
                finally:
                    release.set()
                result = self.finish(g, op)
                self.assertIn(result["phase"], ("cancelled", "fault"), result)
                self.assertEqual(self.board.motions, [])
                g.close()

    def test_partial_write_disarms_no_hold_or_retry_even_after_reconnect(self):
        g = self.gateway()
        real_write = os.write
        with mock.patch("src.arm_motion.os.write", side_effect=lambda fd, data: real_write(fd, data[:1])) as write:
            op = self.jog(g)
            deadline = time.monotonic() + 2
            while g.operation(op["operation_id"])["phase"] != "fault":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(os.read(self.board.read_fd, 100), b"{")
        result = g.operation(op["operation_id"])
        self.assertEqual(result["targets_written"], [])
        self.assertEqual(result["hold_status"], "unavailable")
        g.reconnect()
        self.assertTrue(g.read_state()["observed"])
        with self.assertRaises(ArmGatewayError):
            self.jog(g)

    def test_cancel_after_write_holds_mapped_coordinate_only_once(self):
        entered, release = threading.Event(), threading.Event()
        def lag(board, val):
            if len(board.motions) == 1 and not entered.is_set():
                val["e"] = math.radians(11.74)
                entered.set()
                release.wait(1)
            return val
        self.board.respond = lag
        g = self.gateway()
        op = self.jog(g, delta=1.5)
        self.assertTrue(entered.wait(2))
        try:
            self.assertEqual(g.stop()["hold_status"], "requested")
            g.stop()
        finally:
            release.set()
        result = self.finish(g, op)
        self.assertEqual(result["phase"], "cancelled", result)
        self.assertEqual(result["hold_status"], "written", result)
        self.assertEqual(len(self.board.motions), 2)
        hold = self.board.motions[-1]
        self.assertAlmostEqual(hold["e"], 8.05)
        self.assertEqual((hold["b"], hold["s"], hold["w"], hold["h"]), (10, -4, 6, -16))
        self.assertFalse(result["stop_verified"])
