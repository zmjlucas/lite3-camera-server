"""Ownership and watchdog integration for the bounded local arm gateway."""
import copy
import unittest
from unittest.mock import Mock, patch

from src.control import ControlError, ControlService


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


class ArmOwner:
    """Serial-free operation owner; control must enforce leases before admission."""

    def __init__(self):
        self.active = None
        self.last = None
        self.deadline = None
        self.admitted = 0
        self.closed = False

    def status(self):
        return {'motion_enabled': not self.closed, 'active_operation': copy.deepcopy(self.active),
                'last_operation': copy.deepcopy(self.last), 'reason': 'commissioned diagnostic',
                'connected': True, 'reconnect_required': False,
                'commissioning': {'state': 'ready', 'profile_loaded': True, 'nonce_consumed': True},
                'cached_feedback': None}

    def jog(self, joint, delta, speed, lease_deadline):
        if self.active:
            raise RuntimeError('Arm is busy')
        self.admitted += 1
        self.deadline = lease_deadline
        self.active = {'operation_id': str(self.admitted), 'phase': 'preflight',
                       'joint': joint, 'delta_deg': delta, 'speed_deg_s': speed}
        return copy.deepcopy(self.active)

    def renew(self, lease_deadline):
        self.deadline = lease_deadline

    def stop(self, reason='stop'):
        if self.active:
            self.active['phase'] = 'stopping'
            self.active['reason'] = reason
        return copy.deepcopy(self.active)

    def finish_stop(self):
        self.last, self.active = self.active, None

    def operation(self, operation_id):
        value = self.active or self.last
        if not value or value['operation_id'] != operation_id:
            raise KeyError(operation_id)
        return copy.deepcopy(value)

    def close(self):
        self.stop('shutdown')
        self.closed = True


class ArmControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.arm = ArmOwner()
        self.service = ControlService(mode='hardware', environment='dev', arm_gateway=self.arm,
                                      dog_enabled=False, clock=self.clock)
        self.addCleanup(self.service.close)

    def acquire(self):
        return self.service.dispatch('control/acquire', {'owner': 'arm-test'})['lease']

    def jog(self, lease, seq=1):
        return self.service.dispatch('arm/jog', {'lease': lease, 'seq': seq,
                                               'joint': 'e', 'delta': 2, 'speed': 1})

    def test_arm_only_lifecycle_never_creates_dog_transport(self):
        with patch('src.control.socket.socket', side_effect=AssertionError('dog output forbidden')):
            self.service.start()
            lease = self.acquire()
            result = self.jog(lease)
            self.assertEqual(result['arm']['operation']['phase'], 'preflight')
            self.assertIsNone(result['arm']['position_deg'])
            self.assertFalse(result['dog']['motion_enabled'])
            for action, body in [('dog/stand', {'lease': lease, 'seq': 2}),
                                 ('dog/velocity', {'lease': lease, 'seq': 2, 'vx': .1,
                                                   'vy': 0, 'yaw': 0, 'ttl': .3})]:
                with self.subTest(action=action), self.assertRaises(ControlError) as error:
                    self.service.dispatch(action, body)
                self.assertEqual(error.exception.code, 'locked')
            self.service.dispatch('arm/stop', {})
            self.service._tick()
            self.service.close()
            self.assertFalse(self.service.status()['lease_active'])

    def test_owner_and_sequence_are_checked_before_arm_admission(self):
        lease = self.acquire()
        with self.assertRaises(ControlError) as wrong:
            self.jog('wrong')
        self.assertEqual(wrong.exception.code, 'conflict')
        self.assertEqual(self.arm.admitted, 0)
        self.jog(lease)
        with self.assertRaises(ControlError) as stale:
            self.jog(lease)
        self.assertEqual(stale.exception.code, 'conflict')
        self.assertEqual(self.arm.admitted, 1)

    def test_heartbeat_extends_arm_deadline_and_expiry_cancels(self):
        lease = self.acquire()
        self.jog(lease)
        self.assertEqual(self.arm.deadline, 102.0)
        self.clock.now = 101.0
        self.service.dispatch('control/heartbeat', {'lease': lease})
        self.assertEqual(self.arm.deadline, 103.0)
        self.clock.now = 103.0
        self.service._tick()
        state = self.service.status()
        self.assertFalse(state['lease_active'])
        self.assertEqual(state['arm']['operation']['phase'], 'stopping')

    def test_passive_status_still_expires_lease_and_cancels_operation(self):
        self.jog(self.acquire())
        self.clock.now = 102.0
        state = self.service.status()
        self.assertFalse(state['lease_active'])
        self.assertEqual(state['arm']['operation']['reason'], 'expired')
        self.assertIn('connected', state['arm'])
        self.assertTrue(state['arm']['connected'])
        self.assertIsNone(state['arm']['cached_feedback'])

    def test_stop_release_estop_block_new_owner_until_previous_operation_finishes(self):
        for action in ('arm/stop', 'dog/stop', 'control/release', 'control/estop'):
            with self.subTest(action=action):
                arm = ArmOwner()
                service = ControlService(mode='hardware', environment='dev', arm_gateway=arm,
                                         dog_enabled=False, clock=self.clock)
                try:
                    lease = service.dispatch('control/acquire', {'owner': 'test'})['lease']
                    service.dispatch('arm/jog', {'lease': lease, 'seq': 1, 'joint': 'e',
                                                'delta': 2, 'speed': 1})
                    body = {'lease': lease} if action == 'control/release' else {}
                    stopped = service.dispatch(action, body)
                    self.assertFalse(stopped['lease_active'])
                    self.assertEqual(stopped['arm']['operation']['phase'], 'stopping')
                    with self.assertRaises(ControlError):
                        service.dispatch('control/acquire', {'owner': 'next'})
                    arm.finish_stop()
                    if action == 'control/estop':
                        service.dispatch('control/reset', {'revision': service.status()['safety_revision']})
                    self.assertTrue(service.dispatch('control/acquire', {'owner': 'next'})['lease'])
                finally:
                    service.close()

    def test_operation_lookup_is_read_only_and_unknown_ids_raise_not_found(self):
        lease = self.acquire()
        self.jog(lease)
        op = self.service.dispatch('arm/operation', {'id': '1'})
        self.assertEqual(op['phase'], 'preflight')
        self.assertEqual(self.arm.admitted, 1)
        self.assertTrue(self.service.status()['lease_active'])
        with self.assertRaises(ControlError) as missing:
            self.service.dispatch('arm/operation', {'id': 'other'})
        self.assertEqual(missing.exception.code, 'not_found')

    def test_repeated_shutdown_releases_owned_gateway_once(self):
        with patch.object(self.arm, 'close', wraps=self.arm.close) as close:
            self.service.close()
            self.service.close()
            close.assert_called_once_with()

    def test_gateway_cleanup_failure_still_closes_feedback_and_allows_retry(self):
        feedback = Mock()
        service = ControlService(environment='dev', arm_gateway=self.arm,
                                 dog_enabled=False, dog_state=feedback, clock=self.clock)
        self.addCleanup(service.close)
        with patch.object(self.arm, 'close', side_effect=[RuntimeError('close failed'), None]) as close:
            service.dispatch('control/acquire', {'owner': 'test'})
            with self.assertRaisesRegex(RuntimeError, 'close failed'):
                service.close()
            feedback.close.assert_called_once_with()
            self.assertFalse(service.status()['lease_active'])
            service.close()
            service.close()
            self.assertEqual(close.call_count, 2)
            feedback.close.assert_called_once_with()

    def test_gateway_configuration_requires_hardware_and_isolated_dog_output(self):
        for kwargs in ({'mode': 'hardware', 'dog_enabled': False},
                       {'environment': 'dev', 'simulate': True, 'mode': 'hardware', 'dog_enabled': False},
                       {'environment': 'dev', 'mode': 'readonly', 'dog_enabled': False},
                       {'environment': 'dev', 'mode': 'hardware', 'dog_enabled': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ControlService(arm_gateway=ArmOwner(), **kwargs)


if __name__ == '__main__':
    unittest.main()
