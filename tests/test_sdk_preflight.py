"""Offline SDK rules and guarded requests, without a robot connection."""
import copy
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import lite3_dev_server as sdk
from lite3_dev_server import _responses
import control
from test_dev_client import Response, arm_capabilities, arm_operation, control_status
from tests.test_control import Clock


def dog_capabilities():
    return {
        'actions': {
            'forward': {'axis': 'vx', 'sign': 1, 'max_speed': .2, 'speed_unit': 'm/s'},
            'backward': {'axis': 'vx', 'sign': -1, 'max_speed': .2, 'speed_unit': 'm/s'},
            'turn_left': {'axis': 'yaw', 'sign': -1, 'max_speed': .3, 'speed_unit': 'rad/s'},
            'turn_right': {'axis': 'yaw', 'sign': 1, 'max_speed': .3, 'speed_unit': 'rad/s'},
        },
        'max_duration': 10.,
    }


class ParameterRulesTests(unittest.TestCase):
    def rule(self, name):
        self.assertTrue(callable(getattr(sdk, name, None)), 'Missing public rule: ' + name)
        return getattr(sdk, name)

    def test_offline_directions_map_to_bounded_native_velocity(self):
        validate = self.rule('validate_dog_action')
        for name, expected in (
            ('forward', {'vx': .1, 'vy': 0., 'yaw': 0.}),
            ('backward', {'vx': -.1, 'vy': 0., 'yaw': 0.}),
            ('turn_left', {'vx': 0., 'vy': 0., 'yaw': -.1}),
            ('turn_right', {'vx': 0., 'vy': 0., 'yaw': .1}),
        ):
            self.assertEqual(validate(name, .1, 1.), expected)

    def test_offline_action_rejects_bad_direction_and_numeric_boundaries(self):
        validate = self.rule('validate_dog_action')
        for name in ('left', '', None, True, [], {}):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate(name, .1, 1.)
        for field in ('speed', 'duration'):
            for value in (True, False, float('nan'), float('inf'), -float('inf'),
                          10 ** 1000, '0.1', None, 0, -1, .2001 if field == 'speed' else 10.01):
                values = {'speed': .1, 'duration': 1.}
                values[field] = value
                with self.subTest(field=field, value=str(value)[:20]), self.assertRaises(ValueError):
                    validate('forward', **values)
        validate('forward', .2, 10.)
        validate('turn_right', .3, 10.)

    def test_runtime_action_rules_apply_capabilities_and_axis_limits(self):
        validate = self.rule('validate_dog_action')
        caps = dog_capabilities()
        caps['actions']['forward']['max_speed'] = .08
        caps['max_duration'] = .5
        validate('forward', .08, .5, capabilities=caps)
        for speed, duration in ((.081, .5), (.08, .501)):
            with self.assertRaises(ValueError):
                validate('forward', speed, duration, capabilities=caps)
        with self.assertRaises(ValueError):
            validate('forward', .08, .5, capabilities=caps,
                     limits={'vx': .05, 'vy': .1, 'yaw': .3, 'ttl': .5})
        del caps['actions']['forward']
        with self.assertRaises(ValueError):
            validate('forward', .01, .1, capabilities=caps)

    def test_velocity_and_arm_rules_reject_invalid_values_and_actual_limits(self):
        velocity = self.rule('validate_velocity')
        arm = self.rule('validate_arm_jog')
        velocity(vx=-.2, vy=.1, yaw=-.3, ttl=.5)
        arm('b', -2., 5.)
        for value in (True, float('nan'), float('inf'), 10 ** 1000, '1', None):
            for field in ('vx', 'vy', 'yaw', 'ttl'):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    velocity(**{field: value})
            for field in ('delta', 'speed'):
                values = {'delta': 1., 'speed': 1.}
                values[field] = value
                with self.subTest(field=field), self.assertRaises(ValueError):
                    arm('e', **values)
        for values in ({'ttl': 0}, {'vx': .21}, {'vy': -.11}, {'yaw': .31}, {'ttl': .51}):
            with self.assertRaises(ValueError):
                velocity(**values)
        with self.assertRaises(ValueError):
            velocity(vx=.1, limits={'vx': .05, 'vy': .1, 'yaw': .3, 'ttl': .5})
        for joint, delta, speed in (('x', 1, 1), ('e', 2.1, 1), ('e', 1, 0), ('e', 1, 5.1)):
            with self.assertRaises(ValueError):
                arm(joint, delta, speed)
        with self.assertRaises(ValueError):
            arm('e', 1, 1, position_deg={'b': 0, 's': 0, 'e': 89.5, 'w': 0, 'h': 0})

    def test_diagnostic_arm_rules_enforce_minimum_travel_and_time_budget(self):
        validate = self.rule('validate_arm_jog')
        for delta, speed in ((.05, 1.), (0., 1.), (2., .1), (-2., .1)):
            with self.subTest(delta=delta, speed=speed), self.assertRaises(ValueError):
                validate('e', delta, speed, capabilities=arm_capabilities())
        validate('e', .1, 1., capabilities=arm_capabilities())
        validate('e', -.5, .1, capabilities=arm_capabilities())
        validate('e', .05, 1.)  # Simulation retains its existing small-jog contract.

    def test_server_advertises_its_actual_action_limits(self):
        with patch.dict(control.LIMITS, vx=.08, yaw=.15):
            server = control.ControlService(environment='dev', simulate=True)
            self.addCleanup(server.close)
            dog = server.status()['dog']
            self.assertIn('capabilities', dog)
            caps = dog['capabilities']
            self.assertEqual(caps['actions']['backward'],
                             {'axis': 'vx', 'sign': -1, 'max_speed': .08, 'speed_unit': 'm/s'})
            self.assertEqual(caps['actions']['turn_left']['max_speed'], .15)
            self.assertEqual(caps['max_duration'], 10.)

    def test_additive_capabilities_are_checked_when_present(self):
        old = control_status()
        self.assertIs(_responses.control_status(old), old)
        for field, value in (('max_duration', True), ('max_duration', -1),
                             ('actions', [])):
            state = control_status()
            state['dog']['capabilities'] = dict(dog_capabilities(), **{field: value})
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                _responses.control_status(state)
        for field, value in (('axis', 'vy'), ('sign', True), ('sign', -1),
                             ('max_speed', 0), ('speed_unit', 'rad/s')):
            state = control_status()
            state['dog']['capabilities'] = dog_capabilities()
            state['dog']['capabilities']['actions']['forward'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                _responses.control_status(state)


def ready_status(simulated=True):
    state = control_status(lease_active=True, environment='dev' if simulated else 'dist',
                           simulated=simulated)
    state['dog'].update(phase='ready', motion_enabled=True, actions_enabled=True,
                        capabilities=dog_capabilities())
    state['dog']['feedback'].update(observed=not simulated, fresh=True, standing=True,
                                    motion_ready=True)
    return state


class PreflightTests(unittest.TestCase):
    def check(self, state, operation='dog/action', target='simulation', owns_lease=True, **kwargs):
        self.assertTrue(callable(getattr(sdk, 'check_preflight', None)))
        return sdk.check_preflight(state, operation, target, owns_lease=owns_lease, **kwargs)

    def test_stand_can_prepare_before_action_capability_is_ready(self):
        state = control_status(lease_active=True)
        state['dog']['feedback'].update(standing=False, motion_ready=False, fresh=False)
        original = copy.deepcopy(state)
        self.assertTrue(self.check(state, 'dog/stand')['ok'])
        result = self.check(state)
        self.assertFalse(result['ok'])
        self.assertIn('dog_not_ready', [reason['code'] for reason in result['reasons']])
        self.assertEqual(state, original)

    def test_blockers_cover_control_target_lease_and_feedback(self):
        changes = (
            (('mode',), 'disabled', 'control_disabled'),
            (('mode',), 'readonly', 'control_readonly'),
            (('environment',), 'dist', 'environment_mismatch'),
            (('estop',), True, 'estop_latched'),
            (('lease_active',), False, 'lease_required'),
            (('dog', 'simulated'), False, 'transport_mismatch'),
            (('dog', 'error'), 'Writer failed', 'control_error'),
            (('dog', 'motion_enabled'), False, 'capability_unavailable'),
            (('dog', 'phase'), 'preparing', 'dog_not_ready'),
            (('dog', 'actions_enabled'), False, 'capability_unavailable'),
            (('dog', 'feedback', 'fresh'), False, 'feedback_unavailable'),
            (('dog', 'feedback', 'standing'), False, 'feedback_unavailable'),
            (('dog', 'feedback', 'simulated'), False, 'transport_mismatch'),
        )
        for path, value, code in changes:
            state = ready_status()
            node = state
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            result = self.check(state)
            with self.subTest(path=path):
                self.assertFalse(result['ok'])
                self.assertIn(code, [reason['code'] for reason in result['reasons']])
        self.assertFalse(self.check(ready_status(), owns_lease=False)['ok'])
        self.assertTrue(self.check(ready_status(False), target='hardware')['ok'])
        state = ready_status(False)
        state['dog']['feedback']['observed'] = False
        self.assertFalse(self.check(state, target='hardware')['ok'])

    def test_operation_checks_use_the_requested_device_and_parameters(self):
        state = ready_status()
        state['arm']['simulated'] = False
        self.assertTrue(self.check(state)['ok'])
        self.assertFalse(self.check(state, 'arm/jog')['ok'])
        state = ready_status(False)
        state['environment'] = 'dev'
        state['arm'].update(motion_enabled=True, capabilities=arm_capabilities())
        self.assertTrue(self.check(state, 'arm/jog', target='hardware',
                                   parameters={'joint': 'e', 'delta': 1., 'speed': 1.})['ok'])
        for values in ({'joint': 'b', 'delta': 1., 'speed': 1.},
                       {'joint': 'e', 'delta': 1., 'speed': 2.},
                       {'joint': 's', 'delta': 1., 'speed': 1.}):
            self.assertFalse(self.check(state, 'arm/jog', target='hardware', parameters=values)['ok'])
        state['arm']['operation'] = arm_operation()
        self.assertFalse(self.check(state, 'arm/jog', target='hardware')['ok'])

    def test_missing_capabilities_and_tighter_action_limits_block_timed_action(self):
        state = ready_status()
        state['dog']['capabilities']['max_duration'] = .5
        result = self.check(state, parameters={'name': 'forward', 'speed': .1, 'duration': 1.})
        self.assertEqual([item['code'] for item in result['reasons']], ['invalid_parameters'])
        del state['dog']['capabilities']
        self.assertFalse(self.check(state)['ok'])
        self.assertTrue(self.check(state, 'dog/velocity')['ok'])

    def test_acquisition_does_not_require_motion_readiness_but_checks_ownership(self):
        state = control_status()
        self.assertTrue(self.check(state, 'acquire', owns_lease=False)['ok'])
        state['lease_active'] = True
        result = self.check(state, 'acquire', owns_lease=False)
        self.assertIn('lease_busy', [item['code'] for item in result['reasons']])
        self.assertTrue(self.check(state, 'acquire')['ok'])
        state['lease_active'] = False
        self.assertFalse(self.check(state, 'acquire')['ok'])

    def test_readiness_requires_a_supported_action_or_joint(self):
        state = ready_status()
        state['dog']['capabilities']['actions'] = {'future_action': {}}
        self.assertFalse(self.check(state)['ok'])
        state = ready_status(False)
        state['environment'] = 'dev'
        state['arm'].update(motion_enabled=True, capabilities=arm_capabilities())
        state['arm']['capabilities']['joints'] = []
        self.assertFalse(self.check(state, 'arm/jog', target='hardware')['ok'])


class ServiceEndpoint:
    """Exercise the real control state machine through the client's HTTP boundary."""
    def __init__(self, service):
        self.service = service
        self.calls = []
        self.transform = lambda action, value: value

    def open(self, request, timeout):
        action = request.full_url.split('/api/', 1)[1]
        body = json.loads(request.data) if request.data else None
        self.calls.append((action, body))
        value = self.service.status() if action == 'status' else self.service.dispatch(action, body)
        return Response(self.transform(action, value))


class GuardedClientTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.service = control.ControlService(environment='dev', simulate=True, clock=self.clock)
        self.addCleanup(self.service.close)
        self.endpoint = ServiceEndpoint(self.service)
        patcher = patch(sdk.DevClient.__module__ + '.build_opener', return_value=self.endpoint)
        patcher.start()
        self.addCleanup(patcher.stop)

    def client(self, target='simulation'):
        self.assertTrue(callable(getattr(sdk.DevClient, 'preflight', None)))
        return sdk.DevClient('http://127.0.0.1:8080', target=target)

    def drain_stop(self):
        for _ in range(10):
            self.clock.advance(.06)
            self.service._tick()

    def prepare(self, client):
        client.stand()
        self.service._tick()
        for elapsed in (1.5, 1.5, 1.5, .5, .21, .21, .21):
            self.clock.advance(elapsed)
            client.heartbeat()
            self.service._tick()

    def test_target_mismatch_preflight_and_acquire_never_submit_control(self):
        client = self.client('hardware')
        result = client.preflight('acquire')
        self.assertFalse(result['ok'])
        with self.assertRaises(sdk.PreflightError) as error:
            client.acquire()
        self.assertFalse(error.exception.result['ok'])
        self.assertIsNone(error.exception.status)
        self.assertIsNone(error.exception.code)
        self.assertEqual(self.endpoint.calls, [('status', None), ('status', None)])
        self.assertIsNone(client.lease)

    def test_readonly_preflight_and_explicit_preparation_then_action(self):
        client = self.client()
        self.assertFalse(client.preflight('dog/stand')['ok'])
        acquired = client.acquire()
        before = len(self.endpoint.calls)
        self.assertTrue(client.preflight('dog/stand', status=acquired['status'])['ok'])
        self.assertFalse(client.preflight('dog/action', status=acquired['status'])['ok'])
        self.assertEqual(len(self.endpoint.calls), before)
        with self.assertRaises(sdk.PreflightError):
            client.forward()
        self.prepare(client)
        self.assertTrue(client.preflight('dog/action')['ok'])
        state = client.forward(.1, .5)
        self.assertEqual(state['dog']['action']['name'], 'forward')
        motion = [(action, body['seq']) for action, body in self.endpoint.calls
                  if action in ('dog/stand', 'dog/action')]
        self.assertEqual(motion, [('dog/stand', 1), ('dog/action', 2)])

    def test_acquire_response_drift_releases_only_issued_lease(self):
        def drift(action, value):
            if action == 'control/acquire':
                value['status']['dog']['simulated'] = False
            return value
        self.endpoint.transform = drift
        client = self.client()
        with self.assertRaises(sdk.PreflightError):
            client.acquire()
        self.assertIsNone(client.lease)
        self.assertFalse(self.service.status()['lease_active'])
        self.assertEqual([action for action, _ in self.endpoint.calls],
                         ['status', 'control/acquire', 'control/release'])
        self.assertTrue(self.endpoint.calls[-1][1]['lease'])

    def test_runtime_limits_prevent_motion_posts_and_keep_sequence(self):
        client = self.client()
        client.acquire()
        self.prepare(client)
        with patch.dict(control.LIMITS, vx=.05):
            for call in (lambda: client.forward(.1, 1.), lambda: client.velocity(vx=.1)):
                before = len(self.endpoint.calls)
                with self.assertRaises(sdk.PreflightError):
                    call()
                self.assertEqual(self.endpoint.calls[before:], [('status', None)])
        for call in (lambda: client.forward(True), lambda: client.velocity(ttl=float('nan')),
                     lambda: client.arm_jog('e', 1, False)):
            before = len(self.endpoint.calls)
            with self.assertRaises(sdk.DevClientError):
                call()
            self.assertEqual(len(self.endpoint.calls), before)
        client.velocity(vx=.05)
        self.assertEqual(self.endpoint.calls[-1][1]['seq'], 2)

    def test_guarded_arm_uses_dev_capability_and_actual_position(self):
        client = self.client()
        client.acquire()
        self.assertTrue(client.preflight('arm/jog')['ok'])
        client.arm_jog('e', 1.)
        self.assertEqual(self.service.status()['arm']['position_deg']['e'], 1.)
        self.service._positions['e'] = 89.5
        before = len(self.endpoint.calls)
        with self.assertRaises(sdk.PreflightError):
            client.arm_jog('e', 1.)
        self.assertEqual(self.endpoint.calls[before:], [('status', None)])

    def test_guarded_arm_target_action_submits_once_and_blocks_overlap(self):
        client = self.client()
        client.acquire()
        accepted = client.arm_action({'e': 12.}, duration=1.)
        operation = accepted['arm']['operation']
        self.assertEqual(operation['target_position_deg']['e'], 12.)
        blocked = client.preflight('arm/action')
        self.assertIn('arm_busy', [reason['code'] for reason in blocked['reasons']])
        with self.assertRaises(sdk.PreflightError):
            client.arm_action({'e': 18.}, duration=1.)
        submissions = [body for action, body in self.endpoint.calls if action == 'arm/action']
        self.assertEqual([body['targets'] for body in submissions], [{'e': 12.}])

    def test_delayed_preflight_cannot_send_after_stop_and_reacquire(self):
        client = self.client()
        client.acquire()
        entered, proceed = threading.Event(), threading.Event()
        failures = []

        def delay(action, value):
            if action == 'status' and not entered.is_set():
                entered.set()
                if not proceed.wait(3):
                    raise AssertionError('Timed out waiting to resume preflight')
            return value

        def stand():
            try:
                client.stand()
            except sdk.DevClientError as error:
                failures.append(error)

        self.endpoint.transform = delay
        thread = threading.Thread(target=stand)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            client.stop()
            self.drain_stop()
            client.acquire()
            lease = client.lease
        finally:
            proceed.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertEqual(client.lease, lease)
        self.assertFalse(any(action == 'dog/stand' for action, _ in self.endpoint.calls))

    def test_expired_lease_observation_allows_explicit_reacquisition(self):
        for observe in ('stand', 'status', 'preflight'):
            with self.subTest(observe=observe):
                client = self.client()
                client.acquire()
                self.clock.advance(2.1)
                if observe == 'stand':
                    with self.assertRaises(sdk.PreflightError) as error:
                        client.stand()
                    self.assertIn('lease_required', [r['code'] for r in error.exception.result['reasons']])
                elif observe == 'status':
                    client.status()
                else:
                    self.assertFalse(client.preflight('dog/stand')['ok'])
                self.assertIsNone(client.lease)
                self.drain_stop()
                client.acquire()
                client.release()
                self.drain_stop()

    def test_public_acquire_preflight_rejects_current_or_pending_ownership(self):
        client = self.client()
        client.acquire()
        self.assertFalse(client.preflight('acquire')['ok'])
        client.release()
        self.drain_stop()
        entered, proceed = threading.Event(), threading.Event()
        failures = []

        def delay(action, value):
            if action == 'status' and not entered.is_set():
                entered.set()
                if not proceed.wait(3):
                    raise AssertionError('Timed out waiting for acquisition preflight')
            return value

        def acquire():
            try:
                client.acquire()
            except sdk.DevClientError as error:
                failures.append(error)

        self.endpoint.transform = delay
        thread = threading.Thread(target=acquire)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            result = client.preflight('acquire')
        finally:
            proceed.set()
            thread.join(3)
        self.assertFalse(result['ok'])
        self.assertFalse(failures)

    def test_close_during_acquire_releases_its_late_issued_lease(self):
        client = self.client()
        entered, proceed = threading.Event(), threading.Event()
        failures = []
        issued = []

        def delay(action, value):
            if action == 'control/acquire':
                issued.append(value['lease'])
                entered.set()
                if not proceed.wait(3):
                    raise AssertionError('Timed out waiting for acquired reply')
            return value

        def acquire():
            try:
                client.acquire()
            except sdk.DevClientError as error:
                failures.append(error)

        self.endpoint.transform = delay
        thread = threading.Thread(target=acquire)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            client.close()
        finally:
            proceed.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsNone(client.lease)
        self.assertFalse(self.service.status()['lease_active'])
        self.assertEqual(self.endpoint.calls[-1], ('control/release', {'lease': issued[0]}))


if __name__ == '__main__':
    unittest.main()
