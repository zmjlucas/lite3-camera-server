"""Simulation target timing and ownership; no device transport is opened."""
import unittest
from unittest.mock import patch

from src.application import ControlApplication
from src.control import ControlError, ControlService
from src.lite3_dev_server import _responses
from tests.test_control import Clock


class SimulatedArmActionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.service = ControlService(environment='dev', simulate=True, clock=self.clock)
        self.addCleanup(self.service.close)
        self.lease = self.service.dispatch('control/acquire', {'owner': 'simulation'})['lease']

    def submit(self, **changes):
        body = dict(lease=self.lease, seq=1, targets={'b': 10., 'e': -4.}, duration=1.)
        body.update(changes)
        return self.service.dispatch('arm/action', body)

    def operation(self, operation_id):
        return self.service.dispatch('arm/operation', {'id': operation_id})

    def test_multiple_axes_advance_with_time_and_preserve_omitted_targets(self):
        self.service.dispatch('arm/jog', dict(lease=self.lease, seq=1, joint='w', delta=2, speed=2))
        accepted = self.submit(seq=2)
        operation = accepted['arm']['operation']
        operation_id = operation['operation_id']
        self.assertEqual(operation['phase'], 'moving')
        self.assertEqual(operation['progress'], 0.)
        self.assertEqual(accepted['arm']['position_deg'], dict(b=0., s=0., e=0., w=2., h=0.))
        self.assertTrue(operation['simulated'])
        self.assertFalse(operation['stop_verified'])
        with patch('src.control.socket.socket', side_effect=AssertionError('simulation opened transport')):
            self.clock.now += .25
            self.service._tick()
            self.assertEqual(self.service.status()['arm']['position_deg'],
                             dict(b=2.5, s=0., e=-1., w=2., h=0.))
            mid = self.operation(operation_id)
            self.assertEqual((mid['progress'], mid['elapsed_s']), (.25, .25))
            mid['target_position_deg']['b'] = 180.
            self.clock.now += .75
            self.service._tick()
            final = self.operation(operation_id)
        self.assertEqual(final['phase'], 'completed')
        self.assertEqual(final['position_deg'], dict(b=10., s=0., e=-4., w=2., h=0.))
        self.assertEqual((final['progress'], final['elapsed_s']), (1., 1.))
        self.assertTrue(self.service.status()['lease_active'])
        self.assertEqual(_responses.arm_operation(final), final)
        self.assertEqual(_responses.control_status(self.service.status())['arm']['operation'], final)
        self.assertEqual(_responses.arm_state(ControlApplication(self.service).dispatch('arm/state', {}))
                         ['operation'], final)

    def test_invalid_groups_are_atomic_and_do_not_consume_sequence(self):
        for changes in (
                {'targets': {}}, {'targets': []}, {'targets': None}, {'targets': {'x': 1}},
                {'targets': {'b': 5, 'e': 91}}, {'targets': {'h': -46}},
                {'targets': {'s': -91}}, {'targets': {'w': 91}}, {'targets': {'b': 181}},
                {'targets': {'e': float('nan')}}, {'targets': {'w': float('inf')}},
                {'targets': {'b': 10**1000}}, {'targets': {'e': True}}, {'targets': {'e': '1'}},
                *({'duration': value} for value in (0, -1, 10.01, True, '1', None, float('nan'), float('inf'))),
                {'extra': 'unsupported'}):
            with self.subTest(changes=changes):
                before = self.service.status()
                with self.assertRaises(ControlError) as raised:
                    self.submit(**changes)
                self.assertEqual(raised.exception.code, 'invalid_request')
                self.assertEqual(self.service.status(), before)
        accepted = self.submit(targets={'b': -180., 's': 90., 'e': -90., 'w': 90., 'h': 45.})
        self.assertEqual(accepted['arm']['operation']['phase'], 'moving')

    def test_all_stop_paths_freeze_targets_and_keep_terminal_snapshot(self):
        for action in ('arm/stop', 'dog/stop', 'control/estop', 'control/release', 'control/reset',
                       'expired', 'shutdown'):
            with self.subTest(action=action):
                service = ControlService(environment='dev', simulate=True, clock=self.clock)
                self.addCleanup(service.close)
                lease = service.dispatch('control/acquire', {'owner': 'cancellation'})['lease']
                op = service.dispatch('arm/action', dict(lease=lease, seq=1,
                                                       targets={'b': 12., 'h': -6.}, duration=3))['arm']['operation']
                self.clock.now += .5
                service._tick()
                before = service.status()['arm']['position_deg']
                self.assertEqual(before, dict(b=2., s=0., e=0., w=0., h=-1.))
                if action == 'expired':
                    self.clock.now += 3
                elif action == 'shutdown':
                    service.close()
                else:
                    body = ({'lease': lease} if action == 'control/release' else
                            {'revision': service.status()['safety_revision']} if action == 'control/reset' else {})
                    service.dispatch(action, body)
                service._tick()
                stopped = service.status()['arm']['operation']
                self.assertEqual(stopped['phase'], 'cancelled')
                self.assertEqual(stopped['operation_id'], op['operation_id'])
                self.assertEqual(stopped['position_deg'], before)
                self.clock.now += 100
                service._tick()
                self.assertEqual(service.status()['arm']['position_deg'], before)
                self.assertEqual(service.status()['arm']['operation'], stopped)

if __name__ == '__main__':
    unittest.main()
