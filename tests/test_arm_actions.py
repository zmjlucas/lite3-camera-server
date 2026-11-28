"""Simulation target timing and ownership; no device transport is opened."""
import unittest
from unittest.mock import patch

from src.application import ControlApplication
from src.control import ControlError, ControlService
from src.lite3_dev_server import _responses
from test_arm_control import ArmOwner, Clock


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

    def assert_rejected(self, code, action, body):
        with self.assertRaises(ControlError) as raised:
            self.service.dispatch(action, body)
        self.assertEqual(raised.exception.code, code)

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

    def test_running_action_rejects_competitors_and_jogs_without_replacing_id(self):
        accepted = self.submit()
        operation_id = accepted['arm']['operation']['operation_id']
        for action, body in (
                ('arm/action', dict(lease=self.lease, seq=2, targets={'h': 1}, duration=1)),
                ('arm/jog', dict(lease=self.lease, seq=2, joint='h', delta=1, speed=2))):
            self.assert_rejected('conflict', action, body)
        self.assertEqual(self.operation(operation_id)['progress'], 0)
        self.clock.now += 1
        self.service._tick()
        next_op = self.submit(seq=2)['arm']['operation']
        self.assertNotEqual(next_op['operation_id'], operation_id)
        self.assert_rejected('not_found', 'arm/operation', {'id': operation_id})

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

    def test_lease_expiry_wins_at_completion_deadline_and_stale_lease_cannot_resume(self):
        operation_id = self.submit(duration=2)['arm']['operation']['operation_id']
        self.clock.now += 2
        self.service._tick()
        self.assertEqual(self.operation(operation_id)['phase'], 'cancelled')
        self.assertEqual(self.service.status()['arm']['position_deg']['b'], 0)
        new_lease = self.service.dispatch('control/acquire', {'owner': 'new'})['lease']
        self.assert_rejected('conflict', 'arm/action', dict(lease=self.lease, seq=999,
                                                          targets={'b': 20}, duration=1))
        self.service.dispatch('arm/action', dict(lease=new_lease, seq=1, targets={'b': 2}, duration=1))
        self.clock.now += .5
        self.service._tick()
        self.assertEqual(self.service.status()['arm']['position_deg']['b'], 1)

    def test_heartbeat_allows_action_past_original_lease_deadline(self):
        operation_id = self.submit(duration=3)['arm']['operation']['operation_id']
        self.clock.now += 1.5
        self.service._tick()
        self.service.dispatch('control/heartbeat', {'lease': self.lease})
        self.clock.now += 1.5
        self.service._tick()
        self.assertEqual(self.operation(operation_id)['phase'], 'completed')

    def test_tick_uses_one_timestamp_for_expiry_and_interpolation(self):
        operation_id = self.submit(duration=2)['arm']['operation']['operation_id']
        with patch.object(Clock, '__call__', side_effect=[101.99999, 102., 102.]):
            self.service._tick()
        self.clock.now = 102.
        result = self.operation(operation_id)
        self.assertEqual(result['phase'], 'cancelled')
        self.assertLess(result['position_deg']['b'], 10.)

    def test_action_requires_current_lease_and_sequence(self):
        for changes in ({'lease': 'stale'}, {'seq': 0}, {'seq': True}):
            with self.subTest(changes=changes), self.assertRaises(ControlError):
                self.submit(**changes)
        operation_id = self.submit()['arm']['operation']['operation_id']
        self.clock.now += 1
        self.service._tick()
        with self.assertRaises(ControlError) as raised:
            self.submit()
        self.assertEqual(raised.exception.code, 'conflict')
        self.assertEqual(self.operation(operation_id)['phase'], 'completed')

    def test_action_is_rejected_outside_simulation_before_hardware_admission(self):
        gateway = ArmOwner()
        configurations = ({}, {'environment': 'dev'}, {'mode': 'readonly'}, {'mode': 'disabled'},
                          {'environment': 'dev', 'dog_enabled': False, 'arm_gateway': gateway})
        for options in configurations:
            with self.subTest(options=options):
                service = ControlService(**options)
                self.addCleanup(service.close)
                with self.assertRaises(ControlError) as raised:
                    service.dispatch('arm/action', dict(lease='x', seq=1, targets={'e': 1}, duration=1))
                self.assertIn(raised.exception.code, ('locked', 'unavailable'))
                self.assertIsNone(service._socket)
        self.assertEqual(gateway.admitted, 0)

    def test_unknown_operation_and_invalid_ids_are_rejected(self):
        self.assert_rejected('not_found', 'arm/operation', {'id': 'missing'})
        for value in ('', None, [], 'x' * 129):
            self.assert_rejected('invalid_request', 'arm/operation', {'id': value})


if __name__ == '__main__':
    unittest.main()
