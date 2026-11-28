"""Timed actions exercise the real writer with a clock and a datagram sink."""
import unittest
from unittest.mock import patch

from src import control, dog_state
from tests.test_control import Clock, DatagramSink, prepare_dog
from tests.test_dog_state import state_packet


class DogActionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.service = control.ControlService(clock=self.clock, environment='dev', simulate=True)
        self.addCleanup(self.service.close)

    def ready(self, service=None):
        service = service or self.service
        lease = service.dispatch('control/acquire', {'owner': 'actions'})['lease']
        prepare_dog(service, self.clock, lease)
        return lease

    def action(self, lease, service=None, **values):
        body = dict(lease=lease, seq=2, name='forward', speed=.1, duration=1.0)
        body.update(values)
        return (service or self.service).dispatch('dog/action', body)

    def test_direction_actions_map_to_native_axes_and_complete_with_stop(self):
        for name, vx, yaw in (('forward', .1, 0), ('backward', -.1, 0),
                              ('turn_left', 0, -.1), ('turn_right', 0, .1)):
            with self.subTest(name=name):
                lease = self.ready()
                state = self.action(lease, name=name)
                self.assertEqual(state['dog']['velocity'], dict(vx=vx, vy=0, yaw=yaw))
                self.assertEqual(state['dog']['action']['phase'], 'running')
                self.assertFalse(state['dog']['actions_enabled'])
                self.service._tick()
                self.clock.advance(.9)
                self.service.dispatch('control/heartbeat', {'lease': lease})
                self.assertEqual(self.service.status()['dog']['action']['phase'], 'running')
                self.clock.advance(.1)
                self.service._tick()
                state = self.service.status()
                self.assertEqual(state['dog']['action']['phase'], 'completed')
                self.assertEqual(state['dog']['action']['remaining_s'], 0)
                self.assertEqual(state['dog']['velocity'], dict(vx=0, vy=0, yaw=0))
                self.assertFalse(state['lease_active'])

    def test_stale_feedback_stops_while_normal_walking_is_accepted(self):
        sink = DatagramSink()
        feedback = dog_state.DogStateReceiver('127.0.0.1', clock=self.clock)
        service = control.ControlService(clock=self.clock, dog_host='127.0.0.1', dog_state=feedback)
        self.addCleanup(service.close)
        with patch.object(control.socket, 'socket', return_value=sink):
            lease = self.ready(service)
            feedback._accept_datagram(state_packet(), ('127.0.0.1', 50000))
            self.action(lease, service, duration=5)
            feedback._accept_datagram(state_packet(motion=1), ('127.0.0.1', 50000))
            self.clock.advance(.1)
            service._tick()
            self.assertIn((0x140, .1, 1, ('127.0.0.1', 43893)), sink.packets)
            self.clock.advance(.41)
            service._tick()
            self.assertEqual(service.status()['dog']['action']['phase'], 'fault')
            self.assertIn((control.VOICE, 0, 0, ('127.0.0.1', 43893)), sink.packets)

    def test_lookup_keeps_completed_action_after_a_successor_starts(self):
        first = self.action(self.ready())['dog']['action']
        self.service._tick()
        self.clock.advance(1)
        self.service._tick()
        second = self.action(self.ready(), name='turn_left', speed=.2)['dog']['action']
        expected = dict(first, phase='completed', reason='duration_elapsed', remaining_s=0.0)
        self.assertEqual(self.service.dispatch('dog/action/status', {'id': first['id']}), expected)
        current = self.service.dispatch('dog/action/status', {'id': second['id']})
        self.assertEqual(current, second)
        self.assertEqual(current['phase'], 'running')
        self.assertEqual(self.service.status()['dog']['action'], second)
        current['name'] = 'backward'
        self.assertEqual(self.service.dispatch('dog/action/status', {'id': second['id']}), second)
        self.clock.advance(300)
        with self.assertRaises(control.ControlError) as caught:
            self.service.dispatch('dog/action/status', {'id': first['id']})
        self.assertEqual(caught.exception.code, 'not_found')


if __name__ == '__main__':
    unittest.main()
