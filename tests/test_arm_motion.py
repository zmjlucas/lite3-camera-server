"""Offline, byte-level R5 tests. Synthetic fixtures belong to these tests only."""
import json
from concurrent.futures import ThreadPoolExecutor
import math
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from src.arm_gateway import ArmGatewayError
from src.control import ControlService
from src.remote.arm_worker import SerialSession

from src.arm_motion import ArmMotionGateway


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

    def test_cold_status_stop_close_and_uncommissioned_read(self):
        g = self.gateway(None)
        self.assertFalse(g.status()["motion_enabled"])
        self.assertIsNone(g.stop())
        with self.assertRaises(ArmGatewayError):
            self.jog(g)
        g.close()
        self.assertFalse(self.opens)
        g = self.gateway(None)
        self.assertTrue(g.read_state()["observed"])
        self.assertEqual(self.board.requests, [b'{"T":105}\n'])
        self.assertEqual(self.board.motions, [])

    def test_passive_status_preserves_nonce_and_ages_independent_cached_feedback(self):
        now = [100.0]
        g = self.gateway(clock=lambda: now[0])
        for _ in range(3):
            status = g.status()
            self.assertIn("commissioning", status)
            self.assertEqual(status["commissioning"], {
                "state": "pending_preflight", "profile_loaded": True, "nonce_consumed": False})
            self.assertIsNone(status["cached_feedback"])
            self.assertFalse(status["connected"])
        self.assertEqual(list(self.path.parent.glob(".r5-used-*")), [])
        self.assertEqual(self.opens, [])
        live = g.read_state()
        self.assertEqual(len(list(self.path.parent.glob(".r5-used-*"))), 1)
        self.assertEqual(g.status()["commissioning"], {
            "state": "pending_preflight", "profile_loaded": True, "nonce_consumed": True})
        live["position_deg"]["b"] = -100
        cached = g.status()["cached_feedback"]
        self.assertAlmostEqual(cached["position_deg"]["b"], 10.1)
        self.assertEqual(cached["source"], "local_serial")
        self.assertEqual(cached["sample_monotonic"], 100.0)
        self.assertTrue(cached["observed"])
        self.assertIsNone(cached["gripper_position_deg"])
        self.assertTrue(cached["fresh"])
        cached["position_deg"]["b"] = -100
        now[0] = 103.0
        stale = g.status()["cached_feedback"]
        self.assertEqual(stale["age_s"], 3.0)
        self.assertFalse(stale["fresh"])
        self.assertAlmostEqual(stale["position_deg"]["b"], 10.1)
        self.assertEqual(self.board.requests, [b'{"T":105}\n'])

    def test_fault_and_reconnect_preserve_historical_feedback_and_disable_commissioning(self):
        g = self.gateway()
        g.read_state()
        sampled = g.status().get("cached_feedback")
        self.assertIsNotNone(sampled)
        self.board.respond = lambda board, sample: b""
        with self.assertRaises(ArmGatewayError):
            g.read_state()
        status = g.status()
        self.assertFalse(status["connected"])
        self.assertTrue(status["reconnect_required"])
        self.assertEqual(status["commissioning"]["state"], "recommission_required")
        self.assertEqual(status["cached_feedback"]["sample_monotonic"], sampled["sample_monotonic"])
        self.assertFalse(status["cached_feedback"]["fresh"])
        reads = len(self.board.requests)
        g.reconnect()
        status = g.status()
        self.assertFalse(status["reconnect_required"])
        self.assertFalse(status["connected"])
        self.assertFalse(status["cached_feedback"]["fresh"])
        self.assertEqual(len(self.board.requests), reads)
        self.board.respond = None
        g.read_state()
        self.assertTrue(g.status()["connected"])
        self.assertEqual(g.status()["commissioning"]["state"], "recommission_required")
        self.assertFalse(g.status()["motion_enabled"])

    def test_nonce_status_reflects_a_competing_claim_and_marker_sync_failure(self):
        g = self.gateway()
        other = self.gateway()
        other.read_state()
        with self.assertRaises(ArmGatewayError):
            g.read_state()
        self.assertEqual(g.status()["commissioning"], {
            "state": "recommission_required", "profile_loaded": True, "nonce_consumed": True})
        self.profile["nonce"] = "sync-failure"
        self.save()
        g = self.gateway()
        with mock.patch("src.arm_motion.os.fsync", side_effect=OSError("cannot sync marker")):
            with self.assertRaises(ArmGatewayError):
                g.read_state()
        self.assertTrue(g.status()["commissioning"]["nonce_consumed"])
        self.assertEqual(len(list(self.path.parent.glob(".r5-used-*"))), 2)
        self.assertEqual(self.board.requests, [b'{"T":105}\n'])

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

    def test_bad_jog_and_unreachable_deadline_leave_port_cold(self):
        g = self.gateway()
        for args in (("s", .25, 1), ("b", .51, 1), ("h", .05, 1),
                     ("e", 2.1, 1), ("e", .25, True), ("e", 1, .1),
                     ("e", .2, float("nan"))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.jog(g, *args)
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

    def test_gripper_one_write_unknown_actual_and_anchored_across_operations(self):
        g = self.gateway()
        for i in range(6):
            op = self.jog(g, "h", .5)
            g.renew(time.monotonic() + 12)
            result = self.finish(g, op)
            self.assertEqual(result["phase"], "commanded_unverified", result)
            self.assertIsNone(result["gripper_position_deg"])
            self.assertFalse(result["stop_verified"])
            self.assertEqual(len(self.board.motions), i + 1)
            if i == 0:
                old_id = result["operation_id"]
        with self.assertRaises(KeyError):
            g.operation(old_id)
        with self.assertRaises(ValueError):
            self.jog(g, "h", .1)

    def test_preflight_bad_readings_never_write(self):
        for i, response in enumerate((b"", b'{"T":1051,"b":0,"b":1}\n',
                                      {"T": 1051, "b": float("nan")},
                                      {"T": 1051, "b": 4},
                                      {"T": 1051, "b": 0})):
            self.profile["nonce"] = "bad-reading-{}".format(i)
            self.save()
            g = self.gateway()
            self.board.respond = lambda board, val, r=response: r
            result = self.finish(g, self.jog(g))
            self.assertEqual(result["phase"], "fault", result)
            self.assertFalse(g.status()["motion_enabled"])
            self.assertEqual(self.board.motions, [])
            g.close()

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

    def test_stall_tiny_deadband_wrong_way_and_shoulder_drift_latch(self):
        for kind, joint, delta in (("stall", "e", 1.8), ("deadband", "b", .1),
                                   ("reverse", "e", 1), ("drift", "e", 1)):
            with self.subTest(kind=kind):
                self.profile["nonce"] = kind
                self.save()
                self.board.observed = self.profile["observed_deg"].copy()
                self.board.motions.clear()
                def response(board, val):
                    if board.motions:
                        if kind in ("deadband", "stall"):
                            val[joint] = math.radians(self.profile["observed_deg"][joint])
                        if kind == "reverse":
                            val["e"] = math.radians(11.39)
                        if kind == "drift":
                            val["s"] = math.radians(-3.39)
                    return val
                self.board.respond = response
                g = self.gateway()
                result = self.finish(g, self.jog(g, joint, delta))
                self.assertEqual(result["phase"], "fault", result)
                self.assertFalse(g.status()["motion_enabled"])
                self.assertNotEqual(result["hold_status"], "written")
                for cmd in self.board.motions:
                    self.assertLessEqual(abs(cmd[joint] - self.profile["targets_deg"][joint]), .500001)
                g.close()

    def test_consumed_profile_is_reported_disabled_before_admission(self):
        g = self.gateway()
        g.read_state()
        g.close()
        restarted = self.gateway()
        self.assertFalse(restarted.status()["motion_enabled"])
        self.assertEqual(restarted.status()["commissioning"], {
            "state": "recommission_required", "profile_loaded": True, "nonce_consumed": True})
        self.assertTrue(restarted.read_state()["observed"])
        with self.assertRaises(ArmGatewayError):
            self.jog(restarted)

    def test_expired_lease_and_invalid_renewal_at_final_write_gate(self):
        for why in ("expiry", "stop", "stale", "invalid_renewal"):
            with self.subTest(why=why):
                self.profile["nonce"] = "final-write-" + why
                self.save()
                entered, release = threading.Event(), threading.Event()
                offset = [0]
                g = self.gateway(clock=lambda: time.monotonic() + offset[0])
                def fileno():
                    entered.set()
                    release.wait(2)
                    return self.board.write_fd
                with mock.patch.object(self.board, "fileno", side_effect=fileno):
                    op = self.jog(g)
                    self.assertTrue(entered.wait(2))
                    try:
                        if why == "expiry":
                            offset[0] += 16
                        elif why == "stale":
                            offset[0] += 0.3
                        elif why == "stop":
                            g.stop("observer")
                        else:
                            g.renew(float("inf"))
                    finally:
                        release.set()
                result = self.finish(g, op)
                self.assertIn(result["phase"], ("cancelled", "fault"), result)
                self.assertEqual(self.board.motions, [])
                g.close()

    def test_slow_requested_motion_is_paced_without_false_stall(self):
        g = self.gateway()
        result = self.finish(g, self.jog(g, delta=.5, speed=.1))
        self.assertEqual(result["phase"], "completed", result)
        self.assertAlmostEqual(self.board.motions[-1]["e"], 8.5)

    def test_session_close_can_be_retried_if_retirement_thread_cannot_start(self):
        gateway = self.gateway()
        gateway.read_state()
        service = ControlService(mode="hardware", dog_enabled=False, arm_gateway=gateway, environment="dev")
        with mock.patch.object(self.board, "close", wraps=self.board.close) as close:
            with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("thread limit")):
                with self.assertRaisesRegex(RuntimeError, "thread limit"):
                    service.close()
                close.assert_not_called()
            service.close()
            close.assert_called_once_with()
            self.assertFalse(gateway.status()["connected"])

    def test_slow_device_close_keeps_admission_responsive(self):
        entered, release = threading.Event(), threading.Event()
        g = self.gateway()
        g.read_state()
        def slow_close():
            entered.set()
            release.wait(3)
        with mock.patch.object(self.board, "close", side_effect=slow_close):
            worker = threading.Thread(target=g.close)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                before = time.monotonic()
                self.assertIsNone(g.stop())
                self.assertLess(time.monotonic() - before, .3)
                worker.join(1)
                self.assertFalse(worker.is_alive())
            finally:
                release.set()
                worker.join(1)

    def test_reconnect_failure_and_shutdown_invalidate_cache_before_slow_close(self):
        for action in ('reconnect', 'read_failure', 'close'):
            with self.subTest(action=action):
                self.profile['nonce'] = 'slow-close-' + action
                self.save()
                self.board.respond = None
                g = self.gateway(clock=lambda: 100.0)
                g.read_state()
                entered, release = threading.Event(), threading.Event()
                def slow_close():
                    entered.set()
                    release.wait(2)
                if action == 'read_failure':
                    self.board.respond = lambda board, sample: b''
                    operation = g.read_state
                else:
                    operation = getattr(g, action)
                with mock.patch.object(self.board, 'close', side_effect=slow_close):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        result = pool.submit(operation)
                        try:
                            self.assertTrue(entered.wait(1))
                            status = g.status()
                            self.assertFalse(status['connected'])
                            self.assertFalse(status['cached_feedback']['fresh'])
                            self.assertEqual(status['cached_feedback']['age_s'], 0.0)
                        finally:
                            release.set()
                        if action == 'read_failure':
                            with self.assertRaises(ArmGatewayError):
                                result.result(2)
                        else:
                            result.result(2)

    def test_expiry_and_shutdown_issue_at_most_one_independently_bounded_hold(self):
        for cause in ("expiry", "shutdown"):
            with self.subTest(cause=cause):
                self.profile["nonce"] = "hold-on-" + cause
                self.save()
                self.board.observed = self.profile["observed_deg"].copy()
                self.board.motions.clear()
                entered, release = threading.Event(), threading.Event()
                def pause(board, sample):
                    if len(board.motions) == 1 and not entered.is_set():
                        sample["e"] = math.radians(11.74)
                        entered.set()
                        release.wait(1)
                    return sample
                self.board.respond = pause
                g = self.gateway()
                op = self.jog(g, delta=1.5)
                self.assertTrue(entered.wait(2))
                closer = None
                try:
                    if cause == "expiry":
                        g.renew(time.monotonic() + .025)
                        time.sleep(.04)
                    else:
                        closer = threading.Thread(target=g.close)
                        closer.start()
                        deadline = time.monotonic() + .5
                        while g.operation(op["operation_id"])["phase"] != "stopping":
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(.001)
                finally:
                    release.set()
                result = self.finish(g, op)
                if closer:
                    closer.join(1)
                self.assertEqual(result["phase"], "cancelled", result)
                self.assertEqual(result["hold_status"], "written", result)
                self.assertEqual(len(self.board.motions), 2)
                self.assertAlmostEqual(self.board.motions[-1]["e"], 8.05)
                self.assertFalse(result["stop_verified"])
                g.close()

    def test_commissioned_reads_reject_malformed_and_multiple_reply_frames(self):
        for i, noise in enumerate((b'{"T":1051,"b":0,"b":0}\n', b'{"T":99}\n', None)):
            with self.subTest(noise=noise):
                self.profile["nonce"] = "mixed-frame-{}".format(i)
                self.save()
                def respond(board, sample):
                    good = (json.dumps(sample) + "\n").encode("ascii")
                    return good * 2 if noise is None else noise + good
                self.board.respond = respond
                g = self.gateway()
                result = self.finish(g, self.jog(g))
                self.assertEqual(result["phase"], "fault", result)
                self.assertEqual(self.board.motions, [])
                g.close()

    def test_stale_post_write_feedback_forbids_hold(self):
        entered, release = threading.Event(), threading.Event()
        offset = [0]
        def stale(board, sample):
            if board.motions:
                entered.set()
                release.wait(1)
            return sample
        self.board.respond = stale
        g = self.gateway(clock=lambda: time.monotonic() + offset[0])
        op = self.jog(g, delta=1)
        self.assertTrue(entered.wait(2))
        try:
            g.stop()
            offset[0] += .3
        finally:
            release.set()
        result = self.finish(g, op)
        self.assertEqual(result["phase"], "fault", result)
        self.assertEqual(result["hold_status"], "unavailable")
        self.assertEqual(len(self.board.motions), 1)

    def test_projected_observed_goal_must_fit_its_own_absolute_limit(self):
        self.profile["targets_deg"]["e"] = 89.0
        self.profile["observed_deg"]["e"] = 89.9
        self.save()
        g = self.gateway()
        with self.assertRaises(ValueError):
            self.jog(g, "e", .25)
        self.assertFalse(self.opens)

    def test_rejects_excess_target_to_observed_offset_before_serial_open(self):
        targets = self.profile["targets_deg"].copy()
        measured = self.profile["observed_deg"].copy()
        for joint, target, observed in (
                ("s", 80, -80), ("s", 0, 1.01), ("b", 1, -0.01),
                ("w", -1, 0.01), ("e", -3, 2.01), ("e", 3, -2.01)):
            with self.subTest(joint=joint, target=target, observed=observed):
                self.profile["targets_deg"] = dict(targets, **{joint: target})
                self.profile["observed_deg"] = dict(measured, **{joint: observed})
                self.save()
                with self.assertRaises(ValueError):
                    self.gateway()
        self.assertFalse(self.opens)
        self.board.drain()
        self.assertEqual(self.board.motions, [])
        self.assertEqual(self.board.requests, [])

    def test_accepts_each_inclusive_commissioned_offset_ceiling(self):
        self.profile["observed_deg"] = {"b": 9, "s": -3, "e": 3, "w": 7}
        self.save()
        self.assertTrue(self.gateway().status()["motion_enabled"])
        self.assertFalse(self.opens)

    def test_gripper_dwell_with_continuing_wrist_drift_disarms_after_one_write(self):
        def drift(board, sample):
            if board.motions:
                board.observed["w"] += 0.04
                sample["w"] = math.radians(board.observed["w"])
            return sample

        self.board.respond = drift
        g = self.gateway()
        result = self.finish(g, self.jog(g, "h", .5))
        self.assertEqual(result["phase"], "fault", result)
        self.assertIs(result["observed_stationary"], False)
        self.assertIsNone(result["gripper_position_deg"])
        self.assertFalse(result["stop_verified"])
        self.assertFalse(g.status()["motion_enabled"])
        with self.assertRaises(ArmGatewayError):
            self.jog(g, "h", -.5)
        self.board.drain()
        self.assertEqual(len(self.board.motions), 1)
        self.assertEqual(self.board.motions[0]["h"], -15.5)

    def test_idle_observation_latches_lost_reference_while_reads_remain_available(self):
        for observed, reason in ((35, "envelope"), (11, "target")):
            with self.subTest(observed=observed):
                self.profile["nonce"] = "idle-" + reason
                self.save()
                g = self.gateway()
                self.board.observed["b"] = observed
                state = g.read_state()
                self.assertAlmostEqual(state["position_deg"]["b"], observed)
                self.assertFalse(state["motion_enabled"])
                self.assertIn(reason, state["reason"])
                self.assertFalse(g.status()["reconnect_required"])
                self.assertEqual(g.status()["commissioning"]["state"], "recommission_required")
                self.assertAlmostEqual(g.status()["cached_feedback"]["position_deg"]["b"], observed)
                self.board.observed["b"] = 10.1
                recovered = g.read_state()
                self.assertAlmostEqual(recovered["position_deg"]["b"], 10.1)
                self.assertFalse(recovered["motion_enabled"])
                with self.assertRaises(ArmGatewayError):
                    self.jog(g)
                g.reconnect()
                self.assertFalse(g.read_state()["motion_enabled"])
                with self.assertRaises(ArmGatewayError):
                    self.jog(g)
                self.board.drain()
                self.assertEqual(self.board.motions, [])
                g.close()

    def test_tiny_jog_with_only_prewrite_displacement_faults_as_deadband(self):
        def settle_before_write(board, sample):
            if len(board.requests) > 5:
                sample["e"] = math.radians(11.77)
            return sample

        self.board.respond = settle_before_write
        g = self.gateway()
        result = self.finish(g, self.jog(g, "e", .1))
        self.assertEqual(result["phase"], "fault", result)
        self.assertIn("progress", result["reason"])
        self.assertFalse(g.status()["motion_enabled"])
        self.assertEqual(len(self.board.motions), 1)
        self.assertAlmostEqual(self.board.motions[0]["e"], 8.1)
        self.assertAlmostEqual(result["observed_start"]["position_deg"]["e"], 11.69)
        self.assertAlmostEqual(result["observed_end"]["position_deg"]["e"], 11.77)
