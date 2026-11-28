"""Ownership and watchdog integration for the bounded local arm gateway."""
import copy
import unittest
from unittest.mock import patch

from src.control import ControlError, ControlService
from tests.test_control import Clock


class ArmOwner:
    """Serial-free operation owner; control must enforce leases before admission."""

    def __init__(self):
        self.active = None
        self.deadline = None
        self.admitted = 0
        self.closed = False

    def status(self):
        return {'motion_enabled': not self.closed, 'active_operation': copy.deepcopy(self.active),
                'last_operation': None, 'reason': 'commissioned diagnostic',
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


if __name__ == '__main__':
    unittest.main()
