"""Explicit SDK waiting against the real simulation and controlled HTTP replies."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from src.control import ControlError, ControlService
from test_dev_client import DevClient, DevClientError, Response, arm_operation, protocol_headers
import lite3_dev_server as sdk


class SimulationEndpoint:
    """Keep SDK encoding/validation real while controlling scheduling and transport."""

    def __init__(self):
        self.now = 100.0
        self.service = ControlService(environment='dev', simulate=True, clock=lambda: self.now)
        self.calls = []
        self.before_request = None
        self.after_sleep = None
        self.operation_reply = None

    def open(self, request, timeout):
        action = request.full_url.split('/api/', 1)[1]
        body = json.loads(request.data) if request.data else None
        self.calls.append((action, body, timeout, self.now))
        if self.before_request:
            self.before_request(action, timeout)
        try:
            if action == 'arm/operation' and self.operation_reply:
                result = self.operation_reply()
            else:
                result = self.service.status() if action == 'status' else self.service.dispatch(action, body)
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

    def start_action(self, duration=3):
        self.assertTrue(callable(getattr(self.client, 'arm_action', None)), 'SDK action submission missing')
        self.client.acquire()
        return self.client.arm_action({'b': 12, 'e': -6}, duration)['arm']['operation']['operation_id']

    def wait(self, operation_id, **kwargs):
        self.assertTrue(callable(getattr(self.client, 'wait_arm_operation', None)), 'SDK operation wait missing')
        return self.client.wait_arm_operation(operation_id, **kwargs)

    def test_submit_once_and_wait_renews_existing_lease_until_completion(self):
        operation_id = self.start_action()
        lease = self.client.lease
        result = self.wait(operation_id, timeout=5)
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

    def test_wait_preserves_all_diagnostic_terminal_phases(self):
        for phase in ('completed', 'fault', 'cancelled', 'commanded_unverified'):
            with self.subTest(phase=phase):
                operation = arm_operation()
                operation['phase'] = phase
                operation['reason'] = 'diagnostic result'
                self.endpoint.operation_reply = lambda: operation
                result = self.wait('operation-1')
                self.assertEqual(result, operation)
                self.assertFalse(result['stop_verified'])
                self.assertIsNone(result['gripper_position_deg'])
        self.assertTrue(all(action == 'arm/operation' for action, _, _, _ in self.endpoint.calls))

    def test_stopping_phase_is_polled_until_cancelled(self):
        operation = arm_operation()
        operation['phase'] = 'stopping'
        self.endpoint.operation_reply = lambda: operation
        self.endpoint.after_sleep = lambda: operation.update(phase='cancelled')
        self.assertEqual(self.wait('operation-1')['phase'], 'cancelled')
        self.assertEqual(len(self.endpoint.calls), 2)

    def test_observer_and_renewal_disabled_waits_do_not_acquire_or_renew(self):
        operation_id = self.start_action(duration=.2)
        observer = DevClient(self.client.url)
        self.assertEqual(observer.wait_arm_operation(operation_id)['phase'], 'completed')
        self.assertIsNone(observer.lease)
        operation_id = self.client.arm_action({'h': 1}, 3)['arm']['operation']['operation_id']
        self.assertEqual(self.wait(operation_id, renew_lease=False, timeout=4)['phase'], 'cancelled')
        actions = [row[0] for row in self.endpoint.calls]
        self.assertEqual(actions.count('control/acquire'), 1)
        self.assertNotIn('control/heartbeat', actions)

    def test_timeout_carries_last_snapshot_and_ends_renewal(self):
        operation_id = self.start_action()
        self.assertTrue(hasattr(sdk, 'DevClientTimeout'))
        with self.assertRaises(sdk.DevClientTimeout) as raised:
            self.wait(operation_id, timeout=.6, heartbeat_interval=.2)
        self.assertIsInstance(raised.exception, TimeoutError)
        self.assertIsInstance(raised.exception, DevClientError)
        self.assertEqual(raised.exception.operation_id, operation_id)
        self.assertEqual(raised.exception.operation['phase'], 'moving')
        self.assertAlmostEqual(self.endpoint.now, 100.6)
        count = len(self.endpoint.calls)
        self.endpoint.sleep(3)
        self.assertEqual(len(self.endpoint.calls), count)
        self.assertEqual(self.endpoint.service.status()['arm']['operation']['phase'], 'cancelled')

    def test_request_budget_cannot_exceed_remaining_wait_and_no_retry_occurs(self):
        operation_id = self.start_action()
        lease = self.client.lease
        self.assertTrue(hasattr(sdk, 'DevClientTimeout'))
        def fail_after_timeout(action, timeout):
            if action == 'arm/operation':
                self.assertLessEqual(timeout, .250001)
                self.endpoint.now += timeout
                raise URLError(TimeoutError('read timed out'))
        self.endpoint.before_request = fail_after_timeout
        with self.assertRaises(sdk.DevClientTimeout) as raised:
            self.wait(operation_id, timeout=.25)
        self.assertIsNone(raised.exception.operation)
        self.assertEqual(self.client.lease, lease)
        self.endpoint.before_request = None
        self.assertFalse(self.client.release()['lease_active'])
        self.assertEqual([row[0] for row in self.endpoint.calls],
                         ['control/acquire', 'arm/action', 'arm/operation', 'control/release'])

    def test_external_stop_between_poll_and_heartbeat_returns_cancellation(self):
        operation_id = self.start_action()
        def stop_before_heartbeat(action, timeout):
            if action == 'control/heartbeat':
                self.endpoint.before_request = None
                self.endpoint.service.dispatch('arm/stop', {})
        self.endpoint.before_request = stop_before_heartbeat
        with self.client:
            self.assertEqual(self.wait(operation_id)['phase'], 'cancelled')
        self.assertIsNone(self.client.lease)
        self.assertEqual(sum(row[0] == 'control/heartbeat' for row in self.endpoint.calls), 1)

    def test_cancelled_operation_cleans_up_only_its_acknowledged_session(self):
        operation_id = self.start_action()
        self.endpoint.service.dispatch('arm/stop', {})
        with self.client:
            self.assertEqual(self.wait(operation_id)['phase'], 'cancelled')
        self.assertIsNone(self.client.lease)
        self.client.acquire('new lease')
        lease = self.client.lease
        self.assertEqual(self.wait(operation_id)['phase'], 'cancelled')
        self.assertEqual(self.client.lease, lease)
        self.assertTrue(self.client.heartbeat()['lease_active'])

    def test_late_wrong_id_never_becomes_last_trusted_snapshot(self):
        operation_id = self.start_action()
        unrelated = copy.deepcopy(self.endpoint.service.status()['arm']['operation'])
        unrelated['operation_id'] = 'different-operation'
        def late_reply():
            self.endpoint.now += .3
            return unrelated
        self.endpoint.operation_reply = late_reply
        with self.assertRaises(DevClientError) as raised:
            self.wait(operation_id, timeout=.25)
        if isinstance(raised.exception, sdk.DevClientTimeout):
            self.assertIsNone(raised.exception.operation)

    def test_heartbeat_has_remaining_budget_and_stops_on_failure(self):
        operation_id = self.start_action()
        def fail_heartbeat(action, timeout):
            if action == 'arm/operation':
                self.endpoint.now += .1
            if action == 'control/heartbeat':
                self.assertLessEqual(timeout, .150001)
                raise URLError('offline')
        self.endpoint.before_request = fail_heartbeat
        with self.assertRaises(DevClientError):
            self.wait(operation_id, timeout=.25)
        self.assertIsNone(self.client.lease)
        actions = [row[0] for row in self.endpoint.calls]
        self.assertEqual(actions.count('arm/action'), 1)
        self.assertEqual(actions.count('control/heartbeat'), 1)

    def test_stop_and_new_session_are_never_renewed_by_old_wait(self):
        operation_id = self.start_action()
        lease = self.client.lease
        def change_session():
            self.endpoint.after_sleep = None
            self.client.arm_stop()
            self.client.acquire('new session')
        self.endpoint.after_sleep = change_session
        result = self.wait(operation_id, poll_interval=.7, heartbeat_interval=.1)
        self.assertEqual(result['phase'], 'cancelled')
        self.assertNotEqual(self.client.lease, lease)
        self.assertTrue(all(body['lease'] == lease for action, body, _, _ in self.endpoint.calls
                            if action == 'control/heartbeat'))

    def test_stale_poll_failure_cannot_forget_a_new_session(self):
        operation_id = self.start_action()
        def stale_failure(action, timeout):
            if action == 'arm/operation':
                self.endpoint.before_request = None
                self.client.arm_stop()
                self.client.acquire('replacement')
                raise URLError('old query failed')
        self.endpoint.before_request = stale_failure
        with self.assertRaises(DevClientError):
            self.wait(operation_id)
        self.assertIsNotNone(self.client.lease)
        self.assertTrue(self.client.heartbeat()['lease_active'])

    def test_wrong_id_or_invalid_simulation_payload_is_rejected(self):
        for mutate in (
                lambda op: op.update(operation_id='other'),
                lambda op: op.update(kind='unsupported'),
                lambda op: op.update(simulated=False),
                lambda op: op.update(progress=True),
                lambda op: op['position_deg'].pop('h'),
                lambda op: op['target_position_deg'].update(e=float('nan'))):
            with self.subTest(mutate=mutate):
                self.endpoint.service.dispatch('arm/stop', {})
                self.client = DevClient(self.client.url, timeout=.75)
                operation_id = self.start_action()
                operation = copy.deepcopy(self.endpoint.service.status()['arm']['operation'])
                mutate(operation)
                self.endpoint.operation_reply = lambda: operation
                with self.assertRaises(DevClientError):
                    self.wait(operation_id)
                self.assertIsNone(self.client.lease)
                self.endpoint.operation_reply = None

    def test_invalid_wait_arguments_fail_before_network_io(self):
        self.assertTrue(callable(getattr(self.client, 'wait_arm_operation', None)))
        for field in ('timeout', 'poll_interval', 'heartbeat_interval'):
            for value in (0, -1, True, '1', None, float('nan'), float('inf'), 10**1000):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.wait('operation', **{field: value})
        for changes in ({'heartbeat_interval': 2}, {'renew_lease': 'yes'}):
            with self.assertRaises(ValueError):
                self.wait('operation', **changes)
        for operation_id in ('', None, 123, 'x' * 129):
            with self.assertRaises(ValueError):
                self.wait(operation_id)
        self.assertEqual(self.endpoint.calls, [])


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
