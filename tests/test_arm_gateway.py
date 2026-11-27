"""Read-only R5 tests: fake child pipes and serial, never SSH or hardware."""
import io
import json
import math
import os
import shlex
import threading
import time
import unittest
from unittest import mock

try:
    from src import arm_gateway
    from src.remote import arm_worker
except ImportError:
    arm_gateway = None
    arm_worker = None


STATE = {
    "position_deg": {"b": 90.0, "s": 0.0, "e": -45.0, "w": 0.0},
    "gripper_position_deg": None,
    "observed": True,
    "motion_enabled": False,
    "reason": "Hardware arm motion is disabled.",
}


class FakeProcess:
    """Real OS pipes exercise deadline/select behavior without a child process."""
    def __init__(self, responder):
        request_read, request_write = os.pipe()
        response_read, response_write = os.pipe()
        self.stdin = os.fdopen(request_write, "wb", buffering=0)
        self.stdout = os.fdopen(response_read, "rb", buffering=0)
        self._request = os.fdopen(request_read, "rb", buffering=0)
        self._response = os.fdopen(response_write, "wb", buffering=0)
        self.stopped = threading.Event()
        self.returncode = None
        self.requests = []
        self.thread = threading.Thread(target=self._run, args=(responder,))
        self.thread.daemon = True
        self.thread.start()

    def _run(self, responder):
        try:
            while not self.stopped.is_set():
                line = self._request.readline()
                if not line:
                    break
                request = json.loads(line.decode("utf-8"))
                self.requests.append(request)
                response = responder(request)
                if response is None:
                    self.stopped.wait(2)
                    continue
                if not isinstance(response, bytes):
                    response = (json.dumps(response) + "\n").encode("utf-8")
                self._response.write(response)
        except (OSError, ValueError):
            pass
        finally:
            self._request.close()
            self._response.close()
            self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.stopped.set()
        self.returncode = -15
        if not self.stdin.closed:
            self.stdin.close()

    def kill(self):
        self.terminate()
        self.returncode = -9

    def wait(self, timeout=None):
        self.thread.join(timeout)
        if self.thread.is_alive():
            raise TimeoutError("fake child still running")
        return self.returncode


def good_reply(request):
    return {"id": request["id"], "ok": True, "state": STATE}


@unittest.skipIf(os.name == "nt", "Hardware gateway requires POSIX pipe readiness; unsupported on Windows")
class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(arm_gateway, "read-only arm gateway is not implemented")
        self.processes = []
        self.gateways = []

    def tearDown(self):
        for gateway in self.gateways:
            gateway.close()
        for process in self.processes:
            process.terminate()
            process.wait(1)
            process.stdout.close()

    def factory(self, responder=good_reply):
        def create(*args, **kwargs):
            process = FakeProcess(responder)
            self.processes.append(process)
            return process
        return create

    def gateway(self, **kwargs):
        gateway = arm_gateway.ArmGateway(**kwargs)
        self.gateways.append(gateway)
        return gateway

    def test_read_is_lazy_and_reuses_serial_owner(self):
        with mock.patch.object(arm_gateway.subprocess, "Popen", side_effect=self.factory()) as popen:
            gateway = self.gateway()
            self.assertEqual(popen.call_count, 0)
            self.assertEqual(gateway.read_state(), STATE)
            self.assertEqual(gateway.read_state(), STATE)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual([r["action"] for r in self.processes[0].requests],
                             ["read_state", "read_state"])
            self.assertEqual([r["id"] for r in self.processes[0].requests], [1, 2])

    def test_failure_requires_explicit_reconnect_without_automatic_open(self):
        def fail(request):
            return {"id": request["id"], "ok": False, "error": "serial unavailable"}
        with mock.patch.object(arm_gateway.subprocess, "Popen", side_effect=self.factory(fail)) as popen:
            gateway = self.gateway()
            with self.assertRaises(arm_gateway.ArmGatewayError):
                gateway.read_state()
            with self.assertRaisesRegex(arm_gateway.ArmGatewayError, "reconnect"):
                gateway.read_state()
            self.assertEqual(popen.call_count, 1)
            state = gateway.reconnect()
            self.assertFalse(state["connected"])
            self.assertEqual(popen.call_count, 1)
            popen.side_effect = self.factory()
            self.assertEqual(gateway.read_state(), STATE)
            self.assertEqual(popen.call_count, 2)

    def test_timeout_terminates_child_and_is_latched(self):
        with mock.patch.object(arm_gateway.subprocess, "Popen", side_effect=self.factory(lambda _: None)) as popen:
            gateway = self.gateway(timeout=0.06)
            started = time.monotonic()
            with self.assertRaises(arm_gateway.ArmGatewayError):
                gateway.read_state()
            self.assertLess(time.monotonic() - started, 0.6)
            self.assertTrue(self.processes[0].stopped.is_set())
            with self.assertRaises(arm_gateway.ArmGatewayError):
                gateway.read_state()
            self.assertEqual(popen.call_count, 1)

    def test_oversized_malformed_stale_and_unsafe_worker_replies_fail_closed(self):
        fixtures = [b"x" * 4097, b"not json\n",
                    {"id": 99, "ok": True, "state": STATE},
                    {"id": 1, "ok": True, "state": dict(STATE, motion_enabled=True)},
                    {"id": 1, "ok": True, "state": dict(STATE, observed=False)},
                    {"id": 1, "ok": True, "state": dict(STATE, position_deg={"b": float("nan"), "s": 0, "e": 0, "w": 0})}]
        for fixture in fixtures:
            with self.subTest(fixture=str(fixture)[:50]):
                with mock.patch.object(arm_gateway.subprocess, "Popen", side_effect=self.factory(lambda _, f=fixture: f)):
                    gateway = self.gateway(timeout=0.1)
                    with self.assertRaises(arm_gateway.ArmGatewayError):
                        gateway.read_state()
                    self.assertTrue(self.processes[-1].stopped.is_set())

    def test_serialized_parallel_reads_do_not_interleave_requests(self):
        active = [0]
        max_active = [0]
        def reply(request):
            active[0] += 1
            max_active[0] = max(max_active[0], active[0])
            time.sleep(0.01)
            active[0] -= 1
            return good_reply(request)
        with mock.patch.object(arm_gateway.subprocess, "Popen", side_effect=self.factory(reply)) as popen:
            gateway = self.gateway()
            results = []
            threads = [threading.Thread(target=lambda: results.append(gateway.read_state())) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(1)
            self.assertEqual(results, [STATE] * 4)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual([r["id"] for r in self.processes[0].requests], [1, 2, 3, 4])

    def test_ssh_is_noninteractive_strict_and_runs_fixed_quoted_worker(self):
        with mock.patch.object(arm_gateway.subprocess, "Popen", side_effect=self.factory()) as popen:
            gateway = self.gateway(device="/dev/serial/by-id/usb-r5", host="ysc@192.168.1.103")
            gateway.read_state()
            args, kwargs = popen.call_args
            argv = args[0]
            self.assertEqual(argv[0], "ssh")
            self.assertIn("BatchMode=yes", argv)
            self.assertIn("StrictHostKeyChecking=yes", argv)
            self.assertIn("ConnectTimeout=3", argv)
            self.assertIn("ServerAliveCountMax=2", argv)
            self.assertEqual(argv[-2], "ysc@192.168.1.103")
            remote_argv = shlex.split(argv[-1])
            self.assertEqual(remote_argv[:3], ["python3", "-u", "-c"])
            self.assertEqual(remote_argv[-2:], ["--device", "/dev/serial/by-id/usb-r5"])
            self.assertFalse(kwargs.get("shell", False))
            self.assertEqual(kwargs["stderr"], arm_gateway.subprocess.DEVNULL)

    def test_unsafe_config_is_rejected_before_any_child(self):
        invalid = [{"host": "-oProxyCommand=evil"}, {"host": "robot;whoami"},
                   {"host": "user@host extra"}, {"host": "$(whoami)"},
                   {"device": "/tmp/serial"}, {"device": "/dev/../tmp/serial"},
                   {"device": "/dev/ttyUSB0;echo bad"}, {"timeout": float("nan")},
                   {"timeout": 0}, {"timeout": True}]
        with mock.patch.object(arm_gateway.subprocess, "Popen") as popen:
            for kwargs in invalid:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    self.gateway(**kwargs)
            popen.assert_not_called()

    def test_close_prevents_reopen_even_after_reconnect(self):
        with mock.patch.object(arm_gateway.subprocess, "Popen", side_effect=self.factory()) as popen:
            gateway = self.gateway()
            gateway.read_state()
            gateway.close()
            with self.assertRaises(arm_gateway.ArmGatewayError):
                gateway.read_state()
            with self.assertRaises(arm_gateway.ArmGatewayError):
                gateway.reconnect()
            self.assertEqual(popen.call_count, 1)


class GatewayPlatformTests(unittest.TestCase):
    def test_windows_read_fails_clearly_before_launching_a_worker(self):
        gateway = arm_gateway.ArmGateway()
        self.addCleanup(gateway.close)
        with mock.patch.object(arm_gateway.os, "name", "nt"):
            with mock.patch.object(arm_gateway.subprocess, "Popen",
                                   side_effect=AssertionError("Worker must not start")) as popen:
                with self.assertRaisesRegex(arm_gateway.ArmGatewayError, "Windows"):
                    gateway.read_state()
                popen.assert_not_called()


class FakeSerial:
    def __init__(self, lines):
        self.lines = list(lines)
        self.pending = bytearray()
        self.events = []
        self.writes = []
        self.is_open = False
        self.timeout = 0.01

    def __setattr__(self, name, value):
        if name in ("dtr", "rts", "port") and "events" in self.__dict__:
            self.events.append((name, value))
        object.__setattr__(self, name, value)

    def open(self):
        self.events.append(("open",))
        self.is_open = True

    def close(self):
        self.events.append(("close",))
        self.is_open = False

    def reset_input_buffer(self):
        self.events.append(("reset",))
        self.pending.clear()

    def write(self, payload):
        self.writes.append(payload)
        if self.lines:
            self.pending.extend(self.lines.pop(0))
        return len(payload)

    def read(self, size):
        value = bytes(self.pending[:size])
        del self.pending[:size]
        if not value:
            time.sleep(0.001)
        return value


def feedback(**updates):
    value = {"T": 1051, "b": math.pi / 2, "s": 0, "e": -math.pi / 4, "w": 0, "t": 999}
    value.update(updates)
    return (json.dumps(value) + "\n").encode("ascii")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(arm_worker, "read-only arm worker is not implemented")

    def session(self, fake):
        serial_module = mock.Mock()
        serial_module.Serial.return_value = fake
        patch = mock.patch.dict("sys.modules", {"serial": serial_module})
        patch.start()
        self.addCleanup(patch.stop)
        session = arm_worker.SerialSession("/dev/ttyUSB0", timeout=0.03)
        self.addCleanup(session.close)
        return session, serial_module

    def test_open_configures_dtr_rts_exclusive_before_open_and_reuses_port(self):
        fake = FakeSerial([feedback(), feedback(b=0)])
        session, module = self.session(fake)
        module.Serial.assert_not_called()
        first = session.read_state()
        second = session.read_state()
        self.assertEqual(first["position_deg"], {"b": 90.0, "s": 0.0, "e": -45.0, "w": 0.0})
        self.assertEqual(second["position_deg"]["b"], 0.0)
        self.assertIsNone(first["gripper_position_deg"])
        self.assertFalse(first["motion_enabled"])
        self.assertTrue(first["observed"])
        self.assertEqual(fake.events[:4], [("dtr", False), ("rts", False),
                                          ("port", "/dev/ttyUSB0"), ("open",)])
        self.assertEqual(module.Serial.call_count, 1)
        kwargs = module.Serial.call_args[1]
        self.assertIsNone(kwargs["port"])
        self.assertTrue(kwargs["exclusive"])
        self.assertEqual(kwargs["baudrate"], 115200)
        self.assertEqual([json.loads(v) for v in fake.writes], [{"T": 105}, {"T": 105}])
        self.assertEqual(fake.events.count(("reset",)), 2)

    def test_invalid_feedback_latches_failure_and_never_reopens(self):
        invalid = [feedback(b=True), feedback(e=float("nan")), feedback(w=float("inf")),
                   feedback(b=math.pi + 0.01), feedback(s=2), feedback(e="0"),
                   b'{"T":1051,"b":0,"s":0,"e":0}\n', b'x' * 4097]
        for value in invalid:
            with self.subTest(value=value[:60]):
                fake = FakeSerial([value, feedback()])
                session, module = self.session(fake)
                with self.assertRaises((ValueError, RuntimeError, TimeoutError)):
                    session.read_state()
                with self.assertRaises(RuntimeError):
                    session.read_state()
                self.assertEqual(module.Serial.call_count, 1)
                self.assertFalse(fake.is_open)
                self.assertEqual(len(fake.writes), 1)

    def test_old_buffer_is_discarded_and_nonfeedback_noise_is_bounded(self):
        fake = FakeSerial([b'boot message\n{"T":999}\n' + feedback(b=0)])
        fake.pending.extend(feedback(b=1))
        session, _ = self.session(fake)
        self.assertEqual(session.read_state()["position_deg"]["b"], 0.0)

    def test_missing_reply_times_out_and_closes_owner(self):
        fake = FakeSerial([])
        session, _ = self.session(fake)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            session.read_state()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(fake.is_open)

    def test_worker_rejects_motion_and_unknown_actions_before_serial_open(self):
        requests = [{"id": 1, "action": "jog", "joint": "e", "delta": 1},
                    {"id": 2, "action": "send", "T": 122},
                    {"id": 3, "action": "read_state", "T": 122}]
        incoming = io.BytesIO(b"".join((json.dumps(r) + "\n").encode() for r in requests))
        outgoing = io.BytesIO()
        with mock.patch.dict("sys.modules", {"serial": mock.Mock()}) as modules:
            arm_worker.serve("/dev/ttyUSB0", incoming, outgoing)
            modules["serial"].Serial.assert_not_called()
        replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertTrue(all(reply["ok"] is False for reply in replies))

    def test_worker_protocol_keeps_one_session_and_echoes_request_id(self):
        fake = FakeSerial([feedback(), feedback(b=0)])
        module = mock.Mock()
        module.Serial.return_value = fake
        incoming = io.BytesIO(b'{"id":1,"action":"read_state"}\n{"id":2,"action":"read_state"}\n')
        outgoing = io.BytesIO()
        with mock.patch.dict("sys.modules", {"serial": module}):
            arm_worker.serve("/dev/ttyUSB0", incoming, outgoing)
        replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2])
        self.assertTrue(all(reply["ok"] is True for reply in replies))
        self.assertEqual(replies[1]["state"]["position_deg"]["b"], 0)
        self.assertEqual(module.Serial.call_count, 1)
        self.assertFalse(fake.is_open)


if __name__ == "__main__":
    unittest.main()
