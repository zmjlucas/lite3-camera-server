"""Read-only R5 tests: fake child pipes and serial, never SSH or hardware."""
import io
import json
import os
import threading
import unittest
from unittest import mock

from src import arm_gateway
from src.remote import arm_worker


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

    def test_readonly_status_is_passive_and_keeps_cache_after_reconnect(self):
        now = [100.0]
        with mock.patch.object(arm_gateway.subprocess, "Popen", side_effect=self.factory()):
            gateway = self.gateway(host="robot", clock=lambda: now[0])
            self.assertEqual(gateway.status()["commissioning"], {
                "state": "readonly", "profile_loaded": False, "nonce_consumed": False})
            self.assertIsNone(gateway.status()["cached_feedback"])
            self.assertEqual(self.processes, [])
            live = gateway.read_state()
            live["position_deg"]["b"] = 0
            snapshot = gateway.status()
            self.assertTrue(snapshot["connected"])
            self.assertEqual(snapshot["cached_feedback"]["source"], "ssh_serial")
            self.assertEqual(snapshot["cached_feedback"]["position_deg"]["b"], 90)
            snapshot["cached_feedback"]["position_deg"]["b"] = 0
            now[0] += 2
            self.assertEqual(gateway.status()["cached_feedback"]["age_s"], 2)
            self.assertFalse(gateway.status()["cached_feedback"]["fresh"])
            gateway.reconnect()
            self.assertFalse(gateway.status()["connected"])
            self.assertFalse(gateway.status()["reconnect_required"])
            self.assertEqual(gateway.status()["cached_feedback"]["position_deg"]["b"], 90)
            self.assertEqual(len(self.processes), 1)
            self.assertEqual(len(self.processes[0].requests), 1)

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

class WorkerTests(unittest.TestCase):
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

if __name__ == "__main__":
    unittest.main()
