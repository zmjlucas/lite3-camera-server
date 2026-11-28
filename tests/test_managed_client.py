"""Managed execution contracts at the validated HTTP boundary; no robot I/O."""
import copy
from dataclasses import FrozenInstanceError
import io
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from lite3_dev_server import ArmJog, DogAction, ManagedClient
from tests.test_dev_client import Response, arm_capabilities, arm_operation, control_status, protocol_headers


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
        self.hooks = {}

    def hardware_arm(self):
        self.state['dog'].update(simulated=False, motion_enabled=False)
        self.state['arm'].update(simulated=False, position_deg=None, operation=None,
                                 capabilities=arm_capabilities())

    def count(self, path):
        with self.lock:
            return sum(call[0] == path for call in self.calls)

    def bodies(self, path):
        with self.lock:
            return [call[1] for call in self.calls if call[0] == path]

    def error(self, path, status, message='injected rejection'):
        return HTTPError('http://127.0.0.1/api/' + path, status, message,
                         protocol_headers(), io.BytesIO(json.dumps({'error': message}).encode()))

    def finish(self, phase=None):
        with self.lock:
            action = self.state['dog']['action']
            if action is not None:
                action['phase'] = phase or self.terminal_phase
                action['remaining_s'] = 0.0
                action['reason'] = None if action['phase'] == 'completed' else 'injected terminal state'
                self.state['dog']['phase'] = 'disarmed'
                self.state['dog']['velocity'] = dict(vx=0.0, vy=0.0, yaw=0.0)
                self.state['lease_active'] = False
                self.lease = None

    def open(self, request, timeout):
        path = request.full_url.split('/api/', 1)[1]
        body = json.loads(request.data) if request.data else None
        with self.lock:
            self.calls.append((path, body, timeout, threading.current_thread()))
            hook = self.hooks.get(path)
        if hook is not None:
            value = hook(body)
            if value is not None:
                return Response(value)
        with self.lock:
            if path == 'status':
                action = self.state['dog']['action']
                if self.complete and action is not None and action['phase'] == 'running':
                    self.finish()
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
                value = self.state
            elif path == 'arm/jog':
                self.submissions += 1
                if self.state['arm']['simulated']:
                    self.state['arm']['position_deg'][body['joint']] += body['delta']
                else:
                    operation = arm_operation()
                    operation.update(operation_id='operation-{}'.format(self.submissions),
                                     joint=body['joint'], delta_deg=body['delta'],
                                     speed_deg_s=body['speed'])
                    self.state['arm']['operation'] = operation
                self.submitted.set()
                value = self.state
            elif path == 'arm/operation':
                operation = self.state['arm']['operation']
                if self.complete:
                    operation['phase'] = self.terminal_phase
                    operation['reason'] = None if self.terminal_phase == 'completed' else 'injected terminal state'
                value = operation
            else:
                raise AssertionError('Unexpected endpoint: ' + path)
            return Response(copy.deepcopy(value))


class ManagedClientTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = ManagedEndpoint()
        patcher = patch('lite3_dev_server.client.build_opener', return_value=self.endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, **kwargs):
        options = dict(request_timeout=.05, poll_interval=.002, heartbeat_interval=.01)
        options.update(kwargs)
        client = ManagedClient('http://127.0.0.1:8080', **options)
        self.addCleanup(client.close)
        return client

    def assert_scoped_cleanup(self):
        for path, body, _, _ in self.endpoint.calls:
            if path in ('control/release', 'dog/stop', 'arm/stop'):
                self.assertIn(body, [{'lease': 'lease-{}'.format(n)}
                                     for n in range(1, self.endpoint.acquisitions + 1)])
            self.assertNotIn(path, ('control/reset', 'control/estop', 'arm/reconnect', 'dog/velocity'))

    def assert_renewal_stopped(self):
        workers = {row[3] for row in self.endpoint.calls if row[0] == 'control/heartbeat'}
        self.assertTrue(workers, 'test must exercise at least one background heartbeat')
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def start_run(self, client, sequence=False, timeout=.5):
        outcome = []

        def run():
            try:
                if sequence:
                    outcome.append(client.run_sequence(
                        [DogAction('forward'), DogAction('backward')],
                        policy='completed_only', timeout=timeout))
                else:
                    outcome.append(client.run(DogAction('forward'), timeout=timeout))
            except BaseException as error:
                outcome.append(error)

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 1)
        self.addCleanup(client.cancel)
        return worker, outcome

    def test_action_values_are_frozen(self):
        for action, field, value in ((DogAction('forward'), 'speed', .2),
                                     (ArmJog('b', .1), 'delta', .2)):
            with self.subTest(action=action), self.assertRaises(FrozenInstanceError):
                setattr(action, field, value)

    def test_dog_sequence_uses_fresh_leases_and_preserves_terminal_evidence(self):
        result = self.client().run_sequence(
            [DogAction('forward'), DogAction('turn_left')], policy='completed_only', timeout=.5)
        self.assertEqual(result.phase, 'completed')
        self.assertIsInstance(result.results, tuple)
        self.assertEqual([item.phase for item in result.results], ['completed', 'completed'])
        self.assertEqual([item.action_id for item in result.results], ['action-1', 'action-2'])
        self.assertEqual(result.results[-1].status['dog']['action']['phase'], 'completed')
        self.assertEqual(self.endpoint.count('control/acquire'), 2)
        self.assertEqual([body['lease'] for body in self.endpoint.bodies('dog/action')],
                         ['lease-1', 'lease-2'])
        self.assert_scoped_cleanup()

    def test_invalid_later_action_rejects_entire_sequence_before_acquire(self):
        with self.assertRaises((ValueError, TypeError)):
            self.client().run_sequence([DogAction('forward'), DogAction('jump')],
                                       policy='completed_only')
        self.assertEqual(self.endpoint.count('control/acquire'), 0)
        self.assertEqual(self.endpoint.submissions, 0)

    def test_sequence_requires_explicit_completion_policy(self):
        with self.assertRaises(TypeError):
            self.client().run_sequence([DogAction('forward')])
        self.assertEqual(self.endpoint.count('control/acquire'), 0)

    def test_lost_command_reply_never_replays_and_releases_saved_token(self):
        def lose_reply(body):
            raise URLError('command reply was lost')
        self.endpoint.hooks['dog/action'] = lose_reply
        result = self.client().run_sequence([DogAction('forward'), DogAction('backward')],
                                            policy='completed_only', timeout=.3)
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(len(result.results), 1)
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assert_scoped_cleanup()

    def test_constructor_and_run_reject_nonfinite_or_invalid_static_values(self):
        for field, value in (('owner', ''), ('owner', 'bad\nowner'), ('lease_policy', 'retry'),
                             ('request_timeout', 0), ('request_timeout', float('nan')),
                             ('poll_interval', -1), ('heartbeat_interval', float('inf'))):
            with self.subTest(field=field, value=value), self.assertRaises((ValueError, TypeError)):
                self.client(**{field: value})
        client = self.client()
        for timeout in (0, -1, True, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaises((ValueError, TypeError)):
                client.run(DogAction('forward'), timeout=timeout)
        for make_action in (lambda: DogAction('forward', speed=0),
                            lambda: DogAction('forward', duration=11),
                            lambda: DogAction('forward', speed=True),
                            lambda: ArmJog('x', .1), lambda: ArmJog('b', float('nan')),
                            lambda: ArmJog('b', .1, speed=0)):
            with self.subTest(make_action=make_action), self.assertRaises((ValueError, TypeError)):
                client.run(make_action())
        self.assertEqual(self.endpoint.calls, [])

    def test_empty_sequence_and_unknown_policy_reject_before_io(self):
        for actions, policy in (([], 'completed_only'), ([DogAction('forward')], 'continue')):
            with self.subTest(policy=policy), self.assertRaises((ValueError, TypeError)):
                self.client().run_sequence(actions, policy=policy)
        self.assertEqual(self.endpoint.calls, [])

    def test_capability_or_latch_rejects_whole_sequence_before_acquire(self):
        for field in ('arm_disabled', 'dog_disabled', 'estop', 'readonly'):
            with self.subTest(field=field):
                self.endpoint.state = control_status()
                self.endpoint.calls.clear()
                actions = [DogAction('forward'), ArmJog('b', .1)]
                if field == 'arm_disabled':
                    self.endpoint.state['arm']['motion_enabled'] = False
                elif field == 'dog_disabled':
                    self.endpoint.hardware_arm()
                elif field == 'estop':
                    self.endpoint.state['estop'] = True
                else:
                    self.endpoint.state['mode'] = 'readonly'
                result = self.client().run_sequence(actions, policy='completed_only')
                self.assertEqual(result.phase, 'fault')
                self.assertEqual(result.results, ())
                self.assertEqual(self.endpoint.count('control/acquire'), 0)
                self.assertEqual(self.endpoint.submissions, 0)

    def test_simulated_arm_cumulative_target_validation_precedes_acquire(self):
        self.endpoint.state['arm']['position_deg']['b'] = 179.0
        result = self.client().run_sequence([ArmJog('b', 1.0), ArmJog('b', 1.0)],
                                            policy='completed_only')
        self.assertEqual(result.phase, 'fault')
        self.assertEqual(result.results, ())
        self.assertEqual(self.endpoint.count('control/acquire'), 0)

    def test_hardware_arm_rejects_capability_limits_before_acquire(self):
        self.endpoint.hardware_arm()
        for action in (ArmJog('s', .1), ArmJog('b', .6), ArmJog('b', .2, speed=1.1),
                       ArmJog('b', .09), ArmJog('b', .5, speed=.09)):
            with self.subTest(action=action):
                result = self.client().run(action)
                self.assertEqual(result.phase, 'fault')
                self.assertEqual(self.endpoint.count('control/acquire'), 0)

    def test_hardware_arm_requires_isolated_dog_output(self):
        self.endpoint.hardware_arm()
        self.endpoint.state['dog']['motion_enabled'] = True
        result = self.client().run(ArmJog('b', .1))
        self.assertEqual(result.phase, 'fault')
        self.assertEqual(self.endpoint.count('control/acquire'), 0)

    def test_dog_submission_waits_for_all_readiness_conditions(self):
        for condition in ('phase', 'fresh', 'standing', 'actions_enabled'):
            with self.subTest(condition=condition):
                self.endpoint.state = control_status()
                self.endpoint.calls.clear()

                def incomplete_stand(body):
                    dog = self.endpoint.state['dog']
                    dog.update(phase='ready', actions_enabled=True)
                    if condition == 'phase':
                        dog['phase'] = 'preparing'
                    elif condition == 'actions_enabled':
                        dog['actions_enabled'] = False
                    else:
                        dog['feedback'][condition] = False
                    return copy.deepcopy(self.endpoint.state)

                self.endpoint.hooks['dog/stand'] = incomplete_stand
                result = self.client().run(DogAction('forward'), timeout=.04)
                self.assertEqual(result.phase, 'timed_out')
                self.assertEqual(self.endpoint.count('dog/action'), 0)
                self.assert_scoped_cleanup()

    def test_busy_lease_fail_policy_does_not_prepare_or_retry(self):
        self.endpoint.lease = 'someone-else'
        self.endpoint.state['lease_active'] = True
        result = self.client(lease_policy='fail').run(DogAction('forward'), timeout=.1)
        self.assertEqual(result.phase, 'fault')
        self.assertLessEqual(self.endpoint.count('control/acquire'), 1)
        self.assertEqual(self.endpoint.count('dog/stand'), 0)
        self.assertEqual(self.endpoint.count('control/release'), 0)

    def test_wait_policy_has_total_deadline_while_another_owner_holds_lease(self):
        self.endpoint.lease = 'someone-else'
        self.endpoint.state['lease_active'] = True
        started = time.monotonic()
        result = self.client(lease_policy='wait').run(DogAction('forward'), timeout=.04)
        self.assertEqual(result.phase, 'timed_out')
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(self.endpoint.count('dog/stand'), 0)
        self.assertEqual(self.endpoint.count('control/release'), 0)

    def test_acquire_conflict_waits_but_uncertain_acquire_never_retries(self):
        def conflict(body):
            raise self.endpoint.error('control/acquire', 409, 'Stop is in progress')
        self.endpoint.hooks['control/acquire'] = conflict
        result = self.client(lease_policy='wait').run(DogAction('forward'), timeout=.04)
        self.assertEqual(result.phase, 'timed_out')
        self.assertGreater(self.endpoint.count('control/acquire'), 1)
        self.endpoint.calls.clear()

        def uncertain(body):
            raise URLError('lost acquire response')
        self.endpoint.hooks['control/acquire'] = uncertain
        result = self.client(lease_policy='wait').run(DogAction('forward'), timeout=.1)
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(self.endpoint.count('control/acquire'), 1)
        self.assertEqual(self.endpoint.count('control/release'), 0)

    def test_changed_or_missing_dog_id_halts_sequence(self):
        for replacement in ('another-action', None, ''):
            with self.subTest(replacement=replacement):
                self.endpoint.state = control_status()
                self.endpoint.calls.clear()
                self.endpoint.submitted.clear()

                def changed_status(body):
                    if self.endpoint.submitted.is_set():
                        self.endpoint.finish()
                        if replacement is None:
                            self.endpoint.state['dog']['action'] = None
                        else:
                            self.endpoint.state['dog']['action']['id'] = replacement
                        return copy.deepcopy(self.endpoint.state)
                    return None

                self.endpoint.hooks['status'] = changed_status
                result = self.client().run_sequence([DogAction('forward'), DogAction('backward')],
                                                    policy='completed_only', timeout=.2)
                self.assertEqual(result.phase, 'unknown')
                self.assertEqual(len(result.results), 1)
                self.assertEqual(self.endpoint.count('dog/action'), 1)
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

    def test_hardware_arm_unverified_result_stops_sequence_with_operation_evidence(self):
        self.endpoint.hardware_arm()
        self.endpoint.terminal_phase = 'commanded_unverified'
        result = self.client().run_sequence([ArmJog('h', .1), ArmJog('b', .1)],
                                            policy='completed_only', timeout=.2)
        self.assertEqual(result.phase, 'commanded_unverified')
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].action_id, 'operation-1')
        self.assertEqual(result.results[0].operation['phase'], 'commanded_unverified')
        self.assertEqual(self.endpoint.count('arm/jog'), 1)
        self.assert_scoped_cleanup()

    def test_simulated_arm_completion_is_an_atomic_target_update_without_id(self):
        result = self.client().run(ArmJog('b', .1))
        self.assertEqual(result.phase, 'completed')
        self.assertIsNone(result.action_id)
        self.assertEqual(result.status['arm']['position_deg']['b'], .1)
        self.assertEqual(self.endpoint.count('arm/jog'), 1)
        self.assertEqual(self.endpoint.count('arm/operation'), 0)

    def test_timeout_stops_sequence_and_joins_renewal_worker(self):
        self.endpoint.complete = False
        result = self.client().run_sequence([DogAction('forward'), DogAction('backward')],
                                            policy='completed_only', timeout=.05)
        self.assertEqual(result.phase, 'timed_out')
        self.assertEqual(len(result.results), 1)
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assert_renewal_stopped()
        self.assert_scoped_cleanup()

    def test_cancel_signals_running_sequence_and_joins_renewal_worker(self):
        self.endpoint.complete = False
        client = self.client()
        worker, outcome = self.start_run(client, sequence=True)
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

    def test_close_cancels_and_waits_for_owned_cleanup(self):
        self.endpoint.complete = False
        client = self.client()
        worker, outcome = self.start_run(client)
        self.assertTrue(self.endpoint.submitted.wait(.3))
        self.assertTrue(self.endpoint.heartbeat_seen.wait(.3))
        client.close()
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        worker.join(.3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].phase, 'cancelled')
        self.assert_renewal_stopped()
        client.close()
        self.assertEqual(self.endpoint.count('control/release'), 1)

    def test_keyboard_interrupt_propagates_after_scoped_cleanup_and_worker_join(self):
        self.endpoint.complete = False

        def interrupt(body):
            self.assertTrue(self.endpoint.heartbeat_seen.wait(.3))
            raise KeyboardInterrupt()

        self.endpoint.hooks['dog/action'] = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.client().run(DogAction('forward'), timeout=.5)
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assert_renewal_stopped()
        self.assert_scoped_cleanup()

    def test_stale_token_cleanup_never_escalates_to_observer_stop(self):
        def lose_reply_and_ownership(body):
            self.endpoint.lease = 'new-controller'
            raise URLError('lost action reply')
        self.endpoint.hooks['dog/action'] = lose_reply_and_ownership
        result = self.client().run(DogAction('forward'), timeout=.2)
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assertEqual(self.endpoint.lease, 'new-controller')
        self.assert_scoped_cleanup()

    def heartbeat_completion_race(self, status, replacement=None):
        """The completion read overlaps a renewal reply with a known outcome."""
        completion_read = threading.Event()
        renewal_answer = threading.Event()
        self.endpoint.complete = False

        def heartbeat(body):
            if self.endpoint.submissions == 1:
                self.assertTrue(completion_read.wait(.4))
                self.endpoint.finish()
                if replacement is not None:
                    self.endpoint.state['dog']['action']['id'] = replacement
                renewal_answer.set()
                if status is None:
                    raise URLError('renewal response lost')
                raise self.endpoint.error('control/heartbeat', status)
            return None

        def observe(body):
            if self.endpoint.submissions == 1:
                completion_read.set()
                self.assertTrue(renewal_answer.wait(.4))
                return copy.deepcopy(self.endpoint.state)
            if self.endpoint.submissions > 1:
                self.endpoint.finish()
                return copy.deepcopy(self.endpoint.state)
            return None

        def acquire(body):
            if self.endpoint.acquisitions:
                # A new ownership epoch may start only after the former renewal
                # worker can no longer send requests carrying its old token.
                self.assert_renewal_stopped()
            return None

        self.endpoint.hooks.update({'control/heartbeat': heartbeat, 'status': observe,
                                    'control/acquire': acquire})
        return self.client().run_sequence([DogAction('forward'), DogAction('backward')],
                                          policy='completed_only', timeout=1)

    def test_heartbeat_409_confirms_same_completed_id_before_advancing(self):
        result = self.heartbeat_completion_race(409)
        self.assertEqual(result.phase, 'completed')
        self.assertEqual([item.action_id for item in result.results], ['action-1', 'action-2'])
        self.assertEqual(self.endpoint.count('dog/action'), 2)
        self.assert_renewal_stopped()
        self.assert_scoped_cleanup()

    def test_heartbeat_409_with_changed_id_halts_sequence(self):
        result = self.heartbeat_completion_race(409, replacement='newer-controller-action')
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(len(result.results), 1)
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assert_renewal_stopped()
        self.assert_scoped_cleanup()

    def test_non409_renewal_failure_overrides_concurrent_completion(self):
        result = self.heartbeat_completion_race(503)
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(len(result.results), 1)
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assert_renewal_stopped()
        self.assert_scoped_cleanup()

    def test_lost_renewal_reply_overrides_concurrent_completion(self):
        result = self.heartbeat_completion_race(None)
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(len(result.results), 1)
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assert_renewal_stopped()
        self.assert_scoped_cleanup()

    def test_missing_submission_id_is_unknown_and_never_retried(self):
        self.endpoint.hooks['dog/action'] = lambda body: copy.deepcopy(self.endpoint.state)
        result = self.client().run_sequence([DogAction('forward'), DogAction('backward')],
                                            policy='completed_only', timeout=.2)
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(len(result.results), 1)
        self.assertIsNone(result.results[0].action_id)
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assert_scoped_cleanup()

    def test_capability_is_rechecked_after_stand_before_submission(self):
        def stand_then_disable(body):
            self.endpoint.state['dog'].update(phase='ready', actions_enabled=True,
                                               motion_enabled=False)
            return copy.deepcopy(self.endpoint.state)

        self.endpoint.hooks['dog/stand'] = stand_then_disable
        result = self.client().run(DogAction('forward'), timeout=.1)
        self.assertEqual(result.phase, 'fault')
        self.assertEqual(self.endpoint.count('dog/action'), 0)
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])

    def test_current_simulated_arm_target_is_rechecked_between_steps(self):
        def changed_by_another_controller(body):
            self.endpoint.state['arm']['position_deg']['b'] = 180.0
            return None

        self.endpoint.hooks['control/release'] = changed_by_another_controller
        result = self.client().run_sequence([ArmJog('b', 1.0), ArmJog('b', 1.0)],
                                            policy='completed_only', timeout=.2)
        self.assertEqual(result.phase, 'fault')
        self.assertEqual(self.endpoint.count('arm/jog'), 1)
        self.assert_scoped_cleanup()

    def test_cleanup_failure_is_reported_without_replaying_mutation(self):
        self.endpoint.complete = False

        def failed_release(body):
            raise URLError('release reply lost')

        self.endpoint.hooks['control/release'] = failed_release
        result = self.client().run(DogAction('forward'), timeout=.04)
        self.assertEqual(result.phase, 'timed_out')
        self.assertTrue(result.cleanup_error)
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assertEqual(self.endpoint.count('control/release'), 1)
        self.assert_renewal_stopped()
        self.assert_scoped_cleanup()

    def test_context_manager_closes_after_success(self):
        with self.client() as client:
            result = client.run(ArmJog('b', .1), timeout=.1)
        self.assertEqual(result.phase, 'completed')
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assert_scoped_cleanup()

    def test_cancellation_during_cleanup_is_reported_with_completion_evidence(self):
        client = self.client()
        self.endpoint.hooks['control/release'] = lambda body: client.cancel()
        result = client.run(ArmJog('b', .1))
        self.assertEqual(result.phase, 'cancelled')
        self.assertEqual(result.status['arm']['position_deg']['b'], .1)
        self.assertFalse(self.endpoint.state['lease_active'])

    def test_deadline_during_cleanup_is_reported_with_completion_evidence(self):
        self.endpoint.hooks['control/release'] = lambda body: time.sleep(.04)
        result = self.client().run(ArmJog('b', .1), timeout=.02)
        self.assertEqual(result.phase, 'timed_out')
        self.assertEqual(result.status['arm']['position_deg']['b'], .1)
        self.assertEqual(self.endpoint.count('arm/jog'), 1)

    def test_cancel_during_acquire_releases_token_from_late_reply(self):
        acquiring = threading.Event()
        answer = threading.Event()

        def blocked_acquire(body):
            acquiring.set()
            self.assertTrue(answer.wait(.5))
            return None

        self.endpoint.hooks['control/acquire'] = blocked_acquire
        client = self.client()
        worker, outcome = self.start_run(client)
        self.assertTrue(acquiring.wait(.3))
        client.cancel()
        answer.set()
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].phase, 'cancelled')
        self.assertEqual(self.endpoint.count('dog/stand'), 0)
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assert_scoped_cleanup()

    def test_cancel_during_preparation_does_not_submit_action(self):
        preparing = threading.Event()

        def stand(body):
            self.endpoint.state['dog']['phase'] = 'preparing'
            preparing.set()
            return copy.deepcopy(self.endpoint.state)

        self.endpoint.hooks['dog/stand'] = stand
        client = self.client()
        worker, outcome = self.start_run(client)
        self.assertTrue(preparing.wait(.3))
        self.assertTrue(self.endpoint.heartbeat_seen.wait(.3))
        client.cancel()
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].phase, 'cancelled')
        self.assertEqual(self.endpoint.count('dog/action'), 0)
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assert_renewal_stopped()
        self.assert_scoped_cleanup()

    def test_waiting_for_lease_is_cancellable_without_touching_other_owner(self):
        self.endpoint.lease = 'someone-else'
        self.endpoint.state['lease_active'] = True
        observed = threading.Event()
        self.endpoint.hooks['status'] = lambda body: (observed.set() or None)
        client = self.client(lease_policy='wait')
        worker, outcome = self.start_run(client)
        self.assertTrue(observed.wait(.3))
        client.cancel()
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].phase, 'cancelled')
        self.assertEqual(self.endpoint.count('dog/stand'), 0)
        self.assertEqual(self.endpoint.count('control/release'), 0)
        self.assertEqual(self.endpoint.lease, 'someone-else')

    def test_cancel_after_inflight_command_preserves_acknowledged_id(self):
        command_started = threading.Event()
        answer = threading.Event()

        def blocked_command(body):
            command_started.set()
            self.assertTrue(answer.wait(.5))
            return None

        self.endpoint.complete = False
        self.endpoint.hooks['dog/action'] = blocked_command
        client = self.client()
        worker, outcome = self.start_run(client)
        self.assertTrue(command_started.wait(.3))
        client.cancel()
        answer.set()
        worker.join(.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome[0].phase, 'cancelled')
        self.assertEqual(outcome[0].action_id, 'action-1')
        self.assertEqual(self.endpoint.count('dog/action'), 1)
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assert_scoped_cleanup()

    def test_changed_hardware_operation_id_halts_sequence(self):
        self.endpoint.hardware_arm()

        def changed_operation(body):
            value = copy.deepcopy(self.endpoint.state['arm']['operation'])
            self.assertEqual(body, {'id': 'operation-1'})
            value.update(operation_id='new-controller-operation', phase='completed')
            return value

        self.endpoint.hooks['arm/operation'] = changed_operation
        result = self.client().run_sequence([ArmJog('b', .1), ArmJog('e', .1)],
                                            policy='completed_only', timeout=.2)
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(len(result.results), 1)
        self.assertEqual(result.results[0].action_id, 'operation-1')
        self.assertEqual(self.endpoint.count('arm/jog'), 1)
        self.assert_scoped_cleanup()

    def test_lost_hardware_jog_reply_never_replays_and_releases_owned_token(self):
        self.endpoint.hardware_arm()

        def lost_reply(body):
            raise URLError('arm reply lost')

        self.endpoint.hooks['arm/jog'] = lost_reply
        result = self.client().run_sequence([ArmJog('b', .1), ArmJog('e', .1)],
                                            policy='completed_only', timeout=.2)
        self.assertEqual(result.phase, 'unknown')
        self.assertEqual(len(result.results), 1)
        self.assertEqual(self.endpoint.count('arm/jog'), 1)
        self.assertEqual(self.endpoint.bodies('control/release'), [{'lease': 'lease-1'}])
        self.assert_scoped_cleanup()

    def test_http_timeouts_are_capped_by_remaining_execution_deadline(self):
        self.endpoint.complete = False
        result = self.client(request_timeout=.2).run(DogAction('forward'), timeout=.04)
        self.assertEqual(result.phase, 'timed_out')
        timeouts = [row[2] for row in self.endpoint.calls if row[0] != 'control/release']
        self.assertTrue(timeouts)
        self.assertTrue(all(0 < timeout <= .04 for timeout in timeouts))
        self.assert_scoped_cleanup()

    def test_next_dog_step_waits_for_completed_action_stop_recovery(self):
        recovering_reads = []

        def stop_recovery(body):
            if self.endpoint.submissions == 1:
                self.endpoint.finish()
                recovering_reads.append(True)
                self.endpoint.state['dog']['phase'] = (
                    'stopping' if len(recovering_reads) < 4 else 'disarmed')
                return copy.deepcopy(self.endpoint.state)
            return None

        def acquire_only_after_stop(body):
            self.assertNotEqual(self.endpoint.state['dog']['phase'], 'stopping')
            return None

        self.endpoint.hooks.update({'status': stop_recovery,
                                    'control/acquire': acquire_only_after_stop})
        result = self.client(lease_policy='fail').run_sequence(
            [DogAction('forward'), DogAction('backward')], policy='completed_only', timeout=.3)
        self.assertEqual(result.phase, 'completed')
        self.assertGreaterEqual(len(recovering_reads), 4)
        self.assertEqual(self.endpoint.count('control/acquire'), 2)
        self.assert_scoped_cleanup()


if __name__ == '__main__':
    unittest.main()
