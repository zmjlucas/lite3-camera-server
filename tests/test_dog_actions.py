"""Timed actions exercise the real writer with a clock and a datagram sink."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import control
from tests.test_control import Clock, DatagramSink


class Feedback:
    def __init__(self, clock):
        self.clock = clock
        self.at = clock()
        self.motion = 0
        self.basic = 6
        self.balanced = True

    def snapshot(self):
        age = self.clock() - self.at
        suitable = age <= .5 and self.basic == 6 and self.balanced
        return dict(observed=True, simulated=False, fresh=age <= .5, age_s=age,
                    basic_state=self.basic, gait_state=0, motion_state=self.motion,
                    standing=suitable and self.motion == 0,
                    motion_ready=suitable and self.motion in (0, 1),
                    reason=None if suitable else 'Feedback is stale or unsuitable')

    def start(self):
        return self

    def close(self):
        pass


class DogActionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.service = control.ControlService(clock=self.clock, environment='dev', simulate=True)
        self.addCleanup(self.service.close)

    def ready(self, service=None):
        service = service or self.service
        lease = service.dispatch('control/acquire', {'owner': 'actions'})['lease']
        service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
        service._tick()
        for elapsed in (1.5, 1.5, 1.5, .5, .21, .21, .21):
            self.clock.advance(elapsed)
            service.dispatch('control/heartbeat', {'lease': lease})
            service._tick()
        return lease

    def action(self, lease, service=None, **values):
        body = dict(lease=lease, seq=2, name='forward', speed=.1, duration=1.0)
        body.update(values)
        return (service or self.service).dispatch('dog/action', body)

    def hardware(self):
        sink = DatagramSink()
        patcher = patch.object(control.socket, 'socket', return_value=sink)
        patcher.start()
        self.addCleanup(patcher.stop)
        feedback = Feedback(self.clock)
        service = control.ControlService(clock=self.clock, dog_host='127.0.0.1', dog_state=feedback)
        self.addCleanup(service.close)
        lease = self.ready(service)
        feedback.at = self.clock()
        sink.packets.clear()
        return service, feedback, sink, lease

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

    def test_unprepared_or_unknown_hardware_feedback_rejects_actions(self):
        lease = self.service.dispatch('control/acquire', {'owner': 'test'})['lease']
        with self.assertRaises(control.ControlError) as error:
            self.action(lease)
        self.assertEqual(error.exception.code, 'conflict')
        self.service.dispatch('control/release', {'lease': lease})
        sink = DatagramSink()
        with patch.object(control.socket, 'socket', return_value=sink):
            hardware = control.ControlService(clock=self.clock)
            self.addCleanup(hardware.close)
            lease = self.ready(hardware)
            with self.assertRaises(control.ControlError) as error:
                self.action(lease, hardware)
            self.assertEqual(error.exception.code, 'conflict')
            self.assertFalse(hardware.status()['dog']['actions_enabled'])

    def test_hardware_start_and_each_write_require_fresh_suitable_feedback(self):
        service, feedback, sink, lease = self.hardware()
        feedback.basic = 1
        with self.assertRaises(control.ControlError):
            self.action(lease, service)
        feedback.basic = 6
        feedback.motion = 1
        with self.assertRaises(control.ControlError):
            self.action(lease, service)
        feedback.motion = 0
        self.action(lease, service)
        self.clock.advance(.06)
        # State can change after the heartbeat and before the first axis write.
        def changed(code, value):
            if code == control.HEARTBEAT:
                feedback.basic = 8
        sink.after_send = changed
        service._tick()
        self.assertFalse(any(code in (0x140, 0x145, 0x141) and value != 0
                             for code, value, _, _ in sink.packets))
        self.assertEqual(service.status()['dog']['action']['phase'], 'fault')
        self.assertFalse(service.status()['lease_active'])

    def test_stale_feedback_stops_while_normal_walking_is_accepted(self):
        service, feedback, sink, lease = self.hardware()
        self.action(lease, service, duration=5)
        feedback.motion = 1
        self.clock.advance(.1)
        service._tick()
        self.assertTrue(any(code == 0x140 and value == .1 for code, value, _, _ in sink.packets))
        self.clock.advance(.41)
        service._tick()
        self.assertEqual(service.status()['dog']['action']['phase'], 'fault')
        self.assertIn((control.VOICE, 0), [(code, value) for code, value, _, _ in sink.packets])

    def test_lease_expiry_cancels_long_action_even_with_fresh_simulated_state(self):
        lease = self.ready()
        self.action(lease, duration=10)
        self.clock.advance(2)
        self.service._tick()
        self.assertEqual(self.service.status()['dog']['action']['phase'], 'cancelled')
        self.assertFalse(self.service.status()['lease_active'])

    def test_active_action_rejects_competing_motion_and_stop_invalidates_replays(self):
        lease = self.ready()
        self.action(lease)
        with self.assertRaises(control.ControlError) as error:
            self.action(lease, seq=3)
        self.assertEqual(error.exception.code, 'conflict')
        with self.assertRaises(control.ControlError):
            self.service.dispatch('dog/velocity', dict(lease=lease, seq=3, vx=.1, vy=0, yaw=0, ttl=.3))
        self.service.dispatch('dog/stop', {})
        self.assertEqual(self.service.status()['dog']['action']['phase'], 'cancelled')
        with self.assertRaises(control.ControlError):
            self.action(lease, seq=100)

    def test_action_validation_does_not_consume_the_sequence(self):
        lease = self.ready()
        for values in (dict(name='jump'), dict(name=[]), dict(speed=True), dict(speed=0),
                       dict(speed=-.1), dict(speed=.21), dict(speed=float('nan')),
                       dict(duration=0), dict(duration=11), dict(duration=float('inf')),
                       dict(duration=True), dict(extra=1)):
            with self.subTest(values=values), self.assertRaises(control.ControlError) as error:
                self.action(lease, **values)
            self.assertEqual(error.exception.code, 'invalid_request')
        self.assertEqual(self.action(lease)['dog']['action']['phase'], 'running')

    def test_completion_emits_existing_bounded_hardware_stop(self):
        service, feedback, sink, lease = self.hardware()
        self.action(lease, service, duration=.2)
        service._tick()
        sink.packets.clear()
        self.clock.advance(.2)
        service._tick()
        self.assertEqual(service.status()['dog']['action']['phase'], 'completed')
        self.assertEqual([(code, value) for code, value, _, _ in sink.packets],
                         [(control.VOICE, 0), (control.HEARTBEAT, 0), (0x140, 0),
                          (0x145, 0), (0x141, 0), (control.STATIONARY, 0)])

    def test_short_action_counts_duration_from_first_velocity_output(self):
        service, feedback, sink, lease = self.hardware()
        self.action(lease, service, duration=.025)
        # The writer can wake after the entire requested duration has elapsed.
        self.clock.advance(.06)
        service._tick()
        self.assertTrue(any(code == 0x140 and value == .1 for code, value, _, _ in sink.packets))
        self.assertEqual(service.status()['dog']['action']['phase'], 'running')
        self.clock.advance(.026)
        service._tick()
        self.assertEqual(service.status()['dog']['action']['phase'], 'completed')

    def test_writer_wakes_at_short_action_deadline_without_http_requests(self):
        lease = self.ready()
        self.action(lease, duration=.025)
        waits = []
        def wait(delay):
            waits.append(delay)
            self.clock.advance(delay)
            if self.service._actions.status()['phase'] == 'completed':
                self.service._closed = True
        self.service._wake = SimpleNamespace(wait=wait, clear=lambda: None, set=lambda: None)
        self.service._run()
        self.assertAlmostEqual(waits[0], .025)
        self.assertEqual(self.service.status()['dog']['action']['phase'], 'completed')


if __name__ == '__main__':
    unittest.main()
