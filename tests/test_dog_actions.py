"""Timed actions exercise the real writer with a clock and a datagram sink."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import control
from src.dog_actions import DogActions
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

    def test_lookup_returns_cancellation_and_fault_without_ownership(self):
        for stop in ('dog/stop', 'control/release', 'control/estop', 'shutdown'):
            with self.subTest(stop=stop):
                lease = self.ready()
                action = self.action(lease)['dog']['action']
                if stop == 'shutdown':
                    self.service.close()
                else:
                    self.service.dispatch(stop, {'lease': lease} if stop == 'control/release' else {})
                reason = 'release' if stop == 'control/release' else stop
                result = self.service.dispatch('dog/action/status', {'id': action['id']})
                self.assertEqual(result, dict(action, phase='cancelled', reason=reason, remaining_s=0.0))
                if stop == 'control/estop':
                    self.service.dispatch('control/reset', {'revision': self.service.status()['safety_revision']})

        service, feedback, sink, lease = self.hardware()
        action = self.action(lease, service)['dog']['action']
        sink.fail_codes.add(control.HEARTBEAT)
        service._tick()
        result = service.dispatch('dog/action/status', {'id': action['id']})
        self.assertEqual(result, dict(action, phase='fault', reason='Dog UDP send failed', remaining_s=0.0))

    def test_lookup_validation_and_missing_ids_do_not_change_due_control_state(self):
        service, feedback, sink, lease = self.hardware()
        action = self.action(lease, service)['dog']['action']
        service._tick()
        # The writer alone will act on the due deadline after these reads.
        self.clock.advance(2)
        before = (service._lease, service._lease_deadline, service._sequence, service._generation,
                  service._phase, dict(service._velocity), service._actions.deadline,
                  service._velocity_deadline, service._safety_revision, list(sink.packets))
        service._wake.clear()
        result = service.dispatch('dog/action/status', {'id': action['id']})
        self.assertEqual(result['phase'], 'running')
        self.assertEqual(result['remaining_s'], 0)
        for body, code in (({'id': 'unknown'}, 'not_found'), ({}, 'invalid_request'),
                           ({'id': ''}, 'invalid_request'), ({'id': []}, 'invalid_request'),
                           ({'id': True}, 'invalid_request'), ({'id': 'x' * 129}, 'invalid_request'),
                           ({'id': action['id'], 'lease': lease}, 'invalid_request'),
                           ({'id': action['id'], 'seq': 3}, 'invalid_request')):
            with self.subTest(body=body), self.assertRaises(control.ControlError) as caught:
                service.dispatch('dog/action/status', body)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual((service._lease, service._lease_deadline, service._sequence, service._generation,
                          service._phase, dict(service._velocity), service._actions.deadline,
                          service._velocity_deadline, service._safety_revision, list(sink.packets)), before)
        self.assertFalse(service._wake.is_set())
        service._tick()
        self.assertEqual(service.dispatch('dog/action/status', {'id': action['id']})['reason'], 'expired')


class DogActionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.actions = DogActions(self.clock)

    def test_count_limit_evicts_oldest_terminal_and_keeps_running_action(self):
        completed = []
        for _ in range(129):
            self.actions.start('forward', .1, 1)
            self.actions.finish('completed', 'duration_elapsed')
            completed.append(self.actions.status())
        with self.assertRaises(KeyError):
            self.actions.lookup(completed[0]['id'])
        for record in completed[1:]:
            self.assertEqual(self.actions.lookup(record['id']), record)
        self.actions.start('backward', .2, 10)
        running = self.actions.status()
        self.assertEqual(self.actions.lookup(running['id']), running)
        self.assertEqual(self.actions.lookup(completed[1]['id']), completed[1])
        self.actions.finish('cancelled', 'dog/stop')
        with self.assertRaises(KeyError):
            self.actions.lookup(completed[1]['id'])

    def test_terminal_ttl_starts_at_finish_and_reads_or_stops_never_extend_it(self):
        self.actions.start('turn_right', .2, 10)
        self.actions.mark_started()
        self.clock.advance(10)
        self.actions.finish('fault', 'feedback lost')
        terminal = self.actions.status()
        self.clock.advance(299)
        copy = self.actions.lookup(terminal['id'])
        self.assertEqual(copy, terminal)
        copy['reason'] = 'changed by caller'
        self.assertEqual(self.actions.lookup(terminal['id']), terminal)
        self.actions.finish('cancelled', 'later stop')
        self.clock.advance(1)
        with self.assertRaises(KeyError):
            self.actions.lookup(terminal['id'])
        self.assertEqual(self.actions.status(), terminal)

    def test_expiring_history_keeps_the_running_record_and_its_deadline(self):
        self.actions.start('forward', .1, 1)
        self.actions.finish('completed', 'duration_elapsed')
        first = self.actions.status()
        self.clock.advance(299)
        self.actions.start('turn_left', .2, 10)
        second = self.actions.status()
        self.actions.mark_started()
        deadline = self.actions.deadline
        self.clock.advance(1)
        with self.assertRaises(KeyError):
            self.actions.lookup(first['id'])
        self.assertEqual(self.actions.lookup(second['id']), dict(second, remaining_s=9))
        self.assertEqual(self.actions.deadline, deadline)


if __name__ == '__main__':
    unittest.main()
