"""Explicit SDK waiting against the real simulation and controlled HTTP replies."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

from src.control import ControlError, ControlService
from test_dev_client import DevClient, DevClientError, Response, arm_operation, protocol_headers
import lite3_dev_server as sdk


class SimulationEndpoint:
    """Keep SDK encoding/validation real while controlling scheduling and transport."""

    def __init__(self):
        self.now = 100.0
        self.service = ControlService(environment='dev', simulate=True, clock=lambda: self.now)
        self.calls = []
        self.after_sleep = None

    def open(self, request, timeout):
        action = request.full_url.split('/api/', 1)[1]
        body = json.loads(request.data) if request.data else None
        self.calls.append((action, body, timeout, self.now))
        try:
            result = self.service.dispatch(action, body)
        except ControlError as error:
            code = {'conflict': 409, 'not_found': 404, 'locked': 423}.get(error.code, 400)
            raise HTTPError(request.full_url, code, str(error), protocol_headers(),
                            io.BytesIO(json.dumps({'error': str(error), 'code': error.code}).encode()))
        return Response(result)

    def sleep(self, seconds):
        self.now += seconds
        self.service._tick()
        if self.after_sleep:
            self.after_sleep()


class ArmWaitTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = SimulationEndpoint()
        self.addCleanup(self.endpoint.service.close)
        patcher = patch(DevClient.__module__ + '.build_opener', return_value=self.endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = DevClient('http://127.0.0.1:8080', timeout=.75)
        patcher = patch(DevClient.__module__ + '.time',
                        SimpleNamespace(monotonic=lambda: self.endpoint.now, sleep=self.endpoint.sleep),
                        create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start_action(self):
        self.client.acquire()
        return self.client.arm_action({'b': 12, 'e': -6}, 3)['arm']['operation']['operation_id']

    def test_submit_once_and_wait_renews_existing_lease_until_completion(self):
        operation_id = self.start_action()
        lease = self.client.lease
        result = self.client.wait_arm_operation(operation_id, timeout=5)
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(result['position_deg'], dict(b=12., s=0., e=-6., w=0., h=0.))
        self.assertFalse(result['stop_verified'])
        submissions = [body for action, body, _, _ in self.endpoint.calls if action == 'arm/action']
        self.assertEqual(submissions, [dict(lease=lease, seq=1, targets={'b': 12, 'e': -6}, duration=3)])
        polls = [body for action, body, _, _ in self.endpoint.calls if action == 'arm/operation']
        self.assertGreater(len(polls), 1)
        self.assertTrue(all(body == {'id': operation_id} for body in polls))
        renewals = [body for action, body, _, _ in self.endpoint.calls if action == 'control/heartbeat']
        self.assertGreater(len(renewals), 2)
        self.assertTrue(all(body == {'lease': lease} for body in renewals))
        count = len(self.endpoint.calls)
        self.endpoint.sleep(1)
        self.assertEqual(len(self.endpoint.calls), count)

    def test_timeout_carries_last_snapshot_and_ends_renewal(self):
        operation_id = self.start_action()
        with self.assertRaises(sdk.DevClientTimeout) as raised:
            self.client.wait_arm_operation(operation_id, timeout=.6, heartbeat_interval=.2)
        self.assertIsInstance(raised.exception, TimeoutError)
        self.assertIsInstance(raised.exception, DevClientError)
        self.assertEqual(raised.exception.operation_id, operation_id)
        self.assertEqual(raised.exception.operation['phase'], 'moving')
        self.assertAlmostEqual(self.endpoint.now, 100.6)
        count = len(self.endpoint.calls)
        self.endpoint.sleep(3)
        self.assertEqual(len(self.endpoint.calls), count)
        self.assertEqual(self.endpoint.service.status()['arm']['operation']['phase'], 'cancelled')

    def test_stop_and_new_session_are_never_renewed_by_old_wait(self):
        operation_id = self.start_action()
        lease = self.client.lease
        def change_session():
            self.endpoint.after_sleep = None
            self.client.arm_stop()
            self.client.acquire('new session')
        self.endpoint.after_sleep = change_session
        result = self.client.wait_arm_operation(operation_id, poll_interval=.7, heartbeat_interval=.1)
        self.assertEqual(result['phase'], 'cancelled')
        self.assertNotEqual(self.client.lease, lease)
        self.assertTrue(all(body['lease'] == lease for action, body, _, _ in self.endpoint.calls
                            if action == 'control/heartbeat'))


class ArmWaitHTTPDeadlineTests(unittest.TestCase):
    def test_slow_headers_and_body_are_bounded_by_total_wait_budget(self):
        for slow_part in ('headers', 'body'):
            with self.subTest(slow_part=slow_part):
                stop = threading.Event()
                payload = json.dumps(arm_operation()).encode()

                class TrickleHandler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_POST(self):
                        self.rfile.read(int(self.headers['Content-Length']))
                        header = (b'HTTP/1.1 200 OK\r\nX-Lite3-Dev-Protocol: 1\r\n'
                                  b'Content-Type: application/json\r\nContent-Length: ' +
                                  str(len(payload)).encode() + b'\r\nConnection: close\r\n')
                        try:
                            self.wfile.write(header)
                            if slow_part == 'headers':
                                self.wfile.write(b'X-Slow: ')
                                chunks = [b'x' * 80] * 12
                            else:
                                self.wfile.write(b'\r\n')
                                chunks = [payload[i:i + 50] for i in range(0, len(payload), 50)]
                            for chunk in chunks:
                                self.wfile.write(chunk)
                                if stop.wait(.05):
                                    return
                            if slow_part == 'headers':
                                self.wfile.write(b'\r\n\r\n' + payload)
                        except (BrokenPipeError, ConnectionResetError):
                            pass

                server = ThreadingHTTPServer(('127.0.0.1', 0), TrickleHandler)
                worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
                worker.start()
                try:
                    client = DevClient('http://127.0.0.1:{}'.format(server.server_port), timeout=1)
                    started = time.monotonic()
                    with self.assertRaises(sdk.DevClientTimeout):
                        client.wait_arm_operation('operation-1', timeout=.12)
                    self.assertLess(time.monotonic() - started, .35)
                finally:
                    stop.set()
                    server.shutdown()
                    server.server_close()
                    worker.join(2)


if __name__ == '__main__':
    unittest.main()
