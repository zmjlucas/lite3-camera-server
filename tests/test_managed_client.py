"""Managed execution contracts at the validated HTTP boundary; no robot I/O."""
import copy
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lite3_dev_server import DogAction, ManagedClient
from tests.test_dev_client import Response, control_status, protocol_headers


class ManagedEndpoint:
    """A tiny protocol peer with inspectable requests and explicit race gates."""

    def __init__(self):
        self.calls = []
        self.lock = threading.RLock()
        self.state = control_status()
        self.lease = None
        self.acquisitions = 0
        self.submissions = 0
        self.terminal_phase = 'completed'
        self.complete = True
        self.submitted = threading.Event()
        self.heartbeat_seen = threading.Event()
        self.lose_reply = False

    def count(self, path):
        with self.lock:
            return sum(call[0] == path for call in self.calls)

    def bodies(self, path):
        with self.lock:
            return [call[1] for call in self.calls if call[0] == path]

    def error(self, path, status, message='injected rejection'):
        return HTTPError('http://127.0.0.1/api/' + path, status, message,
                         protocol_headers(), io.BytesIO(json.dumps({'error': message}).encode()))

    def open(self, request, timeout):
        path = request.full_url.split('/api/', 1)[1]
        body = json.loads(request.data) if request.data else None
        with self.lock:
            self.calls.append((path, body, threading.current_thread()))
            if path == 'status':
                action = self.state['dog']['action']
                if self.complete and action is not None and action['phase'] == 'running':
                    action.update(phase=self.terminal_phase, remaining_s=0.0, reason='injected terminal state')
                    self.state['dog']['phase'] = 'disarmed'
                    self.state['lease_active'] = False
                    self.lease = None
                value = self.state
            elif path == 'control/acquire':
                if self.lease is not None:
                    raise self.error(path, 409, 'Control is owned')
                self.acquisitions += 1
                self.lease = 'lease-{}'.format(self.acquisitions)
                self.state['lease_active'] = True
                value = dict(lease=self.lease, status=self.state)
            elif path in ('control/heartbeat', 'control/release'):
                if body.get('lease') != self.lease or self.lease is None:
                    raise self.error(path, 409, 'A current control lease is required')
                if path == 'control/heartbeat':
                    self.heartbeat_seen.set()
                else:
                    self.lease = None
                    self.state['lease_active'] = False
                    self.state['dog']['phase'] = 'disarmed'
                value = self.state
            elif path == 'dog/stand':
                self.state['dog']['phase'] = 'ready'
                self.state['dog']['actions_enabled'] = True
                value = self.state
            elif path == 'dog/action':
                self.submissions += 1
                self.state['dog'].update(phase='moving', actions_enabled=False)
                self.state['dog']['action'] = dict(
                    id='action-{}'.format(self.submissions), name=body['name'],
                    speed=body['speed'], duration=body['duration'], phase='running',
                    reason=None, remaining_s=body['duration'])
                self.submitted.set()
                if self.lose_reply:
                    raise URLError('command reply was lost')
                value = self.state
            else:
                raise AssertionError('Unexpected endpoint: ' + path)
            return Response(copy.deepcopy(value))


class ManagedClientTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = ManagedEndpoint()
        patcher = patch('lite3_dev_server.client.build_opener', return_value=self.endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self):
        client = ManagedClient('http://127.0.0.1:8080', request_timeout=.05,
                               poll_interval=.002, heartbeat_interval=.01)
        self.addCleanup(client.close)
        return client

    def assert_scoped_cleanup(self):
        for path, body, _ in self.endpoint.calls:
            if path in ('control/release', 'dog/stop', 'arm/stop'):
                self.assertIn(body, [{'lease': 'lease-{}'.format(n)}
                                     for n in range(1, self.endpoint.acquisitions + 1)])
            self.assertNotIn(path, ('control/reset', 'control/estop', 'arm/reconnect', 'dog/velocity'))

    def assert_renewal_stopped(self):
        workers = {worker for path, _, worker in self.endpoint.calls if path == 'control/heartbeat'}
        self.assertTrue(workers, 'test must exercise at least one background heartbeat')
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def start_run(self, client):
        outcome = []

        def run():
            try:
                outcome.append(client.run_sequence(
                    [DogAction('forward'), DogAction('backward')],
                    policy='completed_only', timeout=.5))
            except BaseException as error:
                outcome.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 1)
        self.addCleanup(client.cancel)
        return worker, outcome

    def test_lost_command_reply_never_replays_and_releases_saved_token(self):
        self.endpoint.lose_reply = True
        result = self.client().run_sequence([DogAction('forward'), DogAction('backward')],
                                            policy='completed_only', timeout=.3)
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(len(result.results), 1)
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assert_scoped_cleanup()

    def test_cancelled_and_fault_results_stop_sequence(self):
        for phase in ('cancelled', 'fault'):
            with self.subTest(phase=phase):
                self.endpoint.state = control_status()
                self.endpoint.calls.clear()
                self.endpoint.terminal_phase = phase
                result = self.client().run_sequence([DogAction('forward'), DogAction('backward')],
                                                    policy='completed_only', timeout=.2)
                self.assertEqual(result.phase, phase)
                self.assertEqual(len(result.results), 1)
                self.assertEqual(self.endpoint.count('dog/action'), 1)
                self.assertTrue(result.results[0].reason)

    def test_cancel_signals_running_sequence_and_joins_renewal_worker(self):
        self.endpoint.complete = False
        client = self.client()
        worker, outcome = self.start_run(client)
        self.assertTrue(self.endpoint.submitted.wait(.3))
        self.assertTrue(self.endpoint.heartbeat_seen.wait(.3))
        client.cancel()
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].phase, 'cancelled')
        self.assertEqual(len(outcome[0].results), 1)
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assert_renewal_stopped()
        self.assert_scoped_cleanup()


if __name__ == '__main__':
    unittest.main()
