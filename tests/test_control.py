"""Control safety checks use a deterministic clock and an in-memory UDP sink."""
import json
import struct
import threading
import time
import unittest
from unittest.mock import patch

from src import control


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class DatagramSink:
    def __init__(self):
        self.packets = []
        self.timeout = None
        self.closed = False
        self.fail_codes = set()
        self.after_send = None
        self.threads = set()

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendto(self, packet, target):
        self.threads.add(threading.get_ident())
        code, value, flag = struct.unpack('<III', packet[:12])
        amount = struct.unpack('<d', packet[12:])[0] if len(packet) == 20 else value
        self.packets.append((code, amount, flag, target))
        if self.after_send:
            self.after_send(code, amount)
        if code in self.fail_codes:
            raise OSError('injected transport failure')
        return len(packet)

    def close(self):
        self.closed = True


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.service = control.ControlService(clock=self.clock)
        self.addCleanup(self.service.close)

    def acquire(self, service=None):
        return (service or self.service).dispatch('control/acquire', {'owner': 'test'})['lease']

    def assert_error(self, status, action, body, service=None):
        with self.assertRaises(control.ControlError) as caught:
            (service or self.service).dispatch(action, body)
        self.assertEqual(caught.exception.status, status)

    def ready(self, service=None, lease=None):
        service = service or self.service
        lease = lease or self.acquire(service)
        service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
        service._tick()
        for elapsed in (1.5, 1.5, 1.5, 0.5, 0.21, 0.21, 0.21):
            self.clock.advance(elapsed)
            service.dispatch('control/heartbeat', {'lease': lease})
            service._tick()
        self.assertEqual(service.status()['dog']['phase'], 'ready')
        return lease

    def hardware(self):
        sink = DatagramSink()
        patched = patch.object(control.socket, 'socket', return_value=sink)
        patched.start()
        self.addCleanup(patched.stop)
        service = control.ControlService(mode='hardware', dog_host='127.0.0.1', clock=self.clock)
        self.addCleanup(service.close)
        return service, sink

    def test_lease_is_exclusive_unpredictable_and_absent_from_public_status(self):
        lease = self.acquire()
        self.assertGreaterEqual(len(lease), 32)
        self.assert_error(409, 'control/acquire', {'owner': 'second'})
        self.assertNotIn(lease, json.dumps(self.service.status()))
        self.assert_error(409, 'control/heartbeat', {'lease': 'wrong'})
        self.assert_error(409, 'control/heartbeat', {'lease': '无效'})
        self.service.dispatch('control/release', {'lease': lease})
        self.assertNotEqual(lease, self.acquire())

    def test_lease_expiry_invalidates_owner_and_cancels_preparation(self):
        lease = self.acquire()
        self.service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
        self.clock.advance(2.0)
        self.service._tick()
        status = self.service.status()
        self.assertFalse(status['lease_active'])
        self.assertEqual(status['dog']['phase'], 'disarmed')
        self.assert_error(409, 'dog/velocity', {'lease': lease, 'seq': 2, 'vx': .1, 'vy': 0, 'yaw': 0, 'ttl': .5})

    def test_velocity_requires_preparation_and_sequences_reject_replay(self):
        lease = self.acquire()
        velocity = {'lease': lease, 'seq': 2, 'vx': .1, 'vy': 0, 'yaw': 0, 'ttl': .5}
        self.assert_error(409, 'dog/velocity', velocity)
        self.ready(lease=lease)
        self.service.dispatch('dog/velocity', velocity)
        self.assertEqual(self.service.status()['dog']['velocity']['vx'], .1)
        self.assert_error(409, 'dog/velocity', velocity)
        self.assert_error(409, 'dog/stand', {'lease': lease, 'seq': 1})

    def test_heartbeat_does_not_extend_velocity_deadline(self):
        lease = self.ready()
        self.service.dispatch('dog/velocity', {'lease': lease, 'seq': 2, 'vx': .2, 'vy': -.1, 'yaw': .3, 'ttl': .5})
        self.clock.advance(.4)
        self.service.dispatch('control/heartbeat', {'lease': lease})
        self.clock.advance(.1)
        self.service._tick()
        status = self.service.status()
        self.assertEqual(status['dog']['velocity'], {'vx': 0.0, 'vy': 0.0, 'yaw': 0.0})
        self.assertFalse(status['lease_active'])
        self.assertEqual(status['dog']['phase'], 'disarmed')

    def test_observer_stop_revokes_lease_and_rejects_delayed_high_sequence_motion(self):
        lease = self.ready()
        self.service.dispatch('dog/stop', {})
        self.assert_error(409, 'dog/stand', {'lease': lease, 'seq': 500})
        self.assert_error(409, 'arm/jog', {'lease': lease, 'seq': 501, 'joint': 'b', 'delta': 1, 'speed': 2})
        self.assertFalse(self.service.status()['lease_active'])

    def test_estop_is_latched_and_reset_does_not_restore_lease(self):
        lease = self.acquire()
        self.service.dispatch('control/estop', {})
        self.assertTrue(self.service.status()['estop'])
        self.assert_error(423, 'control/acquire', {'owner': 'test'})
        revision = self.service.status().get('safety_revision')
        self.service.dispatch('control/reset', {'revision': revision})
        self.assertFalse(self.service.status()['estop'])
        self.assertFalse(self.service.status()['lease_active'])
        self.assert_error(409, 'control/heartbeat', {'lease': lease})

    def test_reset_requires_an_observed_revision(self):
        self.service.dispatch('control/estop', {})
        for body in ({}, {'revision': None}, {'revision': True}, {'revision': 1},
                     {'revision': []}, {'revision': {}}, {'revision': ''},
                     {'revision': '无效'}, {'revision': 'x' * 129}):
            with self.subTest(body=body):
                self.assert_error(400, 'control/reset', body)
                self.assertTrue(self.service.status()['estop'])

    def test_delayed_reset_cannot_clear_a_newer_estop_after_stop_completes(self):
        service, sink = self.hardware()
        service.dispatch('control/estop', {})
        previous = service.status().get('safety_revision')
        delayed_reset = {'revision': previous}
        # Another operator stops again while the first reset is in flight.
        service.dispatch('control/estop', {})
        current = service.status().get('safety_revision')
        self.assertNotEqual(previous, current)
        for _ in range(control.STOP_BURSTS):
            service._tick()
            self.clock.advance(.051)
        self.assertEqual(service.status()['dog']['phase'], 'disarmed')
        self.assert_error(409, 'control/reset', delayed_reset, service)
        self.assertTrue(service.status()['estop'])
        self.assert_error(423, 'control/acquire', {'owner': 'test'}, service)
        self.assertEqual(len(sink.packets), control.STOP_BURSTS * 5)
        service.dispatch('control/reset', {'revision': current})
        self.assertFalse(service.status()['estop'])
        self.assertFalse(service.status()['lease_active'])

    def test_reset_revision_cannot_be_replayed_against_a_new_owner(self):
        self.service.dispatch('control/estop', {})
        revision = self.service.status().get('safety_revision')
        self.service.dispatch('control/reset', {'revision': revision})
        self.assertNotEqual(revision, self.service.status().get('safety_revision'))
        lease = self.acquire()
        self.assert_error(409, 'control/reset', {'revision': revision})
        self.service.dispatch('control/heartbeat', {'lease': lease})

    def test_revision_from_previous_server_cannot_reset_a_restarted_service(self):
        previous = self.service.status().get('safety_revision')
        restarted = control.ControlService(clock=self.clock)
        self.addCleanup(restarted.close)
        self.assertNotEqual(previous, restarted.status().get('safety_revision'))
        self.assert_error(409, 'control/reset', {'revision': previous}, restarted)

    def test_new_transport_failure_invalidates_reset_but_repeated_failure_does_not(self):
        service, sink = self.hardware()
        lease = self.acquire(service)
        service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
        previous = service.status().get('safety_revision')
        sink.fail_codes.add(control.HEARTBEAT)
        service._tick()
        current = service.status().get('safety_revision')
        self.assertNotEqual(previous, current)
        for _ in range(control.STOP_BURSTS):
            service._tick()
            self.clock.advance(.051)
        self.assertEqual(current, service.status().get('safety_revision'))
        self.assert_error(409, 'control/reset', {'revision': previous}, service)
        self.assertIsNotNone(service.status()['dog']['error'])
        sink.fail_codes.clear()
        service.dispatch('control/reset', {'revision': current})
        self.assertIsNone(service.status()['dog']['error'])

    def test_stale_scoped_stop_cannot_revoke_a_new_owner(self):
        old = self.acquire()
        self.service.dispatch('control/release', {'lease': old})
        current = self.acquire()
        self.service.dispatch('dog/stand', {'lease': current, 'seq': 1})
        for supplied in (old, 'unknown', None, []):
            with self.subTest(lease=supplied):
                self.assert_error(409, 'dog/stop', {'lease': supplied})
                self.assertTrue(self.service.status()['lease_active'])
                self.assertEqual(self.service.status()['dog']['phase'], 'preparing')
        self.service.dispatch('dog/stop', {'lease': current})
        self.assertFalse(self.service.status()['lease_active'])

    def test_observer_stop_and_estop_still_stop_any_owner(self):
        self.acquire()
        self.service.dispatch('dog/stop', {})
        self.assertFalse(self.service.status()['lease_active'])
        self.acquire()
        self.service.dispatch('control/estop', {})
        self.assertFalse(self.service.status()['lease_active'])
        self.assertTrue(self.service.status()['estop'])

    def test_validation_rejects_nonfinite_boolean_and_out_of_range_motion(self):
        lease = self.ready()
        valid = {'lease': lease, 'seq': 2, 'vx': 0, 'vy': 0, 'yaw': 0, 'ttl': .5}
        for key, value in [('vx', .201), ('vy', -.101), ('yaw', .301), ('ttl', .501),
                           ('ttl', 0), ('vx', True), ('vx', float('nan')),
                           ('yaw', float('inf')), ('vx', 10 ** 1000), ('seq', True), ('seq', 2.5)]:
            body = dict(valid)
            body[key] = value
            with self.subTest(key=key, value=value):
                self.assert_error(400, 'dog/velocity', body)
        self.service.dispatch('dog/velocity', valid)

    def test_simulated_arm_limits_and_sequence_protect_targets(self):
        lease = self.acquire()
        body = {'lease': lease, 'seq': 1, 'joint': 'b', 'delta': 2, 'speed': 5}
        self.service.dispatch('arm/jog', body)
        self.assertEqual(self.service.status()['arm']['position_deg']['b'], 2)
        self.assert_error(409, 'arm/jog', body)
        for key, value in [('joint', 'unknown'), ('delta', 2.1), ('delta', True),
                           ('speed', 5.1), ('speed', 0), ('delta', float('nan'))]:
            invalid = dict(body, seq=2)
            invalid[key] = value
            self.assert_error(400, 'arm/jog', invalid)
        for seq in range(2, 91):
            self.service.dispatch('arm/jog', dict(body, seq=seq))
        self.assertEqual(self.service.status()['arm']['position_deg']['b'], 180)
        self.assert_error(400, 'arm/jog', dict(body, seq=91))

    def test_hardware_arm_is_blocked_without_opening_any_transport(self):
        service, sink = self.hardware()
        self.assert_error(423, 'arm/jog', {'joint': 'b', 'delta': 1, 'speed': 1}, service)
        self.assertEqual(sink.packets, [])
        self.assertFalse(service.status()['arm']['motion_enabled'])
        self.assertEqual(service.status()['arm']['reason'],
                         'Hardware arm motion is disabled.')

    def test_simulation_opens_no_sockets_through_full_motion_flow(self):
        with patch.object(control.socket, 'socket', side_effect=AssertionError('simulation opened socket')):
            lease = self.ready()
            self.service.dispatch('dog/velocity', {'lease': lease, 'seq': 2, 'vx': .1, 'vy': 0, 'yaw': 0, 'ttl': .5})
            self.service._tick()
            self.service.dispatch('arm/jog', {'lease': lease, 'seq': 3, 'joint': 'h', 'delta': -2, 'speed': 2})
            self.service.dispatch('dog/stop', {})
            self.service.close()
        self.assertFalse(self.service.status()['dog']['stop_verified'])

    def test_staged_stand_protocol_and_velocity_wire_format(self):
        service, sink = self.hardware()
        lease = self.ready(service)
        stage_codes = [p[0] for p in sink.packets if p[0] not in (0x21040001, 0x0140, 0x0145, 0x0141)]
        self.assertEqual(stage_codes, [0x21010D05, 0x21010C0A, 0x21010D06, 0x21010300, 0x21010C03])
        self.assertIn((0x21010C0A, 1, 0, ('127.0.0.1', 43893)), sink.packets)
        service.dispatch('dog/velocity', {'lease': lease, 'seq': 2, 'vx': .2, 'vy': -.1, 'yaw': .3, 'ttl': .5})
        self.clock.advance(.05)
        service._tick()
        self.assertIn((0x0140, .2, 1, ('127.0.0.1', 43893)), sink.packets)
        self.assertIn((0x0145, -.1, 1, ('127.0.0.1', 43893)), sink.packets)
        self.assertIn((0x0141, .3, 1, ('127.0.0.1', 43893)), sink.packets)
        self.assertLessEqual(sink.timeout, .02)
        self.assertEqual(len(sink.threads), 1)

    def test_stop_cancels_all_later_stand_stages_and_attempts_each_packet(self):
        service, sink = self.hardware()
        lease = self.acquire(service)
        service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
        service._tick()
        service.dispatch('dog/stop', {})
        sink.packets[:] = []
        sink.fail_codes.add(0x0140)
        for _ in range(10):
            service._tick()
            self.clock.advance(.051)
        codes = [packet[0] for packet in sink.packets]
        self.assertEqual(codes.count(0x0140), 10)
        self.assertEqual(codes.count(0x0145), 10)
        self.assertEqual(codes.count(0x0141), 10)
        self.assertEqual(codes.count(0x21010D05), 10)
        self.assertNotIn(0x21010D06, codes)
        self.assertNotIn(0x21010300, codes)
        self.assertEqual(service.status()['dog']['phase'], 'error')
        self.assertIsNotNone(service.status()['dog']['error'])
        self.assert_error(423, 'control/acquire', {'owner': 'test'}, service)
        sink.fail_codes.clear()
        service.dispatch('control/reset', {'revision': service.status().get('safety_revision')})
        self.assertFalse(service.status()['lease_active'])
        self.assertIsNone(service.status()['dog']['error'])

    def test_transport_error_latches_and_cancels_nonzero_packets(self):
        service, sink = self.hardware()
        lease = self.ready(service)
        service.dispatch('dog/velocity', {'lease': lease, 'seq': 2, 'vx': .2, 'vy': .1, 'yaw': .3, 'ttl': .5})
        sink.fail_codes.add(0x21040001)
        sink.packets[:] = []
        self.clock.advance(.05)
        service._tick()
        self.assertFalse(any(p[0] in (0x0140, 0x0145, 0x0141) and p[1] != 0 for p in sink.packets))
        self.assertFalse(service.status()['lease_active'])
        self.assertIsNotNone(service.status()['dog']['error'])

    def test_expiry_between_axis_sends_prevents_next_nonzero_axis(self):
        service, sink = self.hardware()
        lease = self.ready(service)
        service.dispatch('dog/velocity', {'lease': lease, 'seq': 2, 'vx': .2, 'vy': .1, 'yaw': .3, 'ttl': .1})
        sink.packets[:] = []
        def expire_after_first_axis(code, amount):
            if code in (0x0140, 0x0145, 0x0141) and amount:
                self.clock.advance(.2)
        sink.after_send = expire_after_first_axis
        self.clock.advance(.05)
        service._tick()
        nonzero = [p for p in sink.packets if p[0] in (0x0140, 0x0145, 0x0141) and p[1] != 0]
        self.assertEqual(len(nonzero), 1)
        self.assertFalse(service.status()['lease_active'])

    def test_real_worker_shutdown_stops_and_closes_socket(self):
        sink = DatagramSink()
        with patch.object(control.socket, 'socket', return_value=sink):
            service = control.ControlService(mode='hardware', dog_host='127.0.0.1')
            self.addCleanup(service.close)
            service.start()
            lease = self.acquire(service)
            service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
            service.close()
        self.assertTrue(sink.closed)
        self.assertEqual(len(sink.threads), 1)
        self.assertIn(0x21010D05, [p[0] for p in sink.packets])
        self.assertFalse(service.status()['lease_active'])
        self.assert_error(503, 'control/acquire', {'owner': 'test'}, service)

    def test_failed_socket_configuration_never_uses_an_unbounded_socket(self):
        service, sink = self.hardware()
        def fail_timeout(timeout):
            raise OSError('Cannot configure timeout')
        sink.settimeout = fail_timeout
        lease = self.acquire(service)
        service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
        service._tick()
        self.clock.advance(.05)
        service._tick()
        self.assertEqual(sink.packets, [])
        self.assertTrue(sink.closed)
        self.assertIsNotNone(service.status()['dog']['error'])

    def test_updated_velocity_cancels_remaining_packets_from_older_command(self):
        service, sink = self.hardware()
        lease = self.ready(service)
        service.dispatch('dog/velocity', {'lease': lease, 'seq': 2, 'vx': .2, 'vy': .1, 'yaw': .3, 'ttl': .5})
        sink.packets[:] = []
        def replace_after_first_axis(code, amount):
            if code == 0x0140 and amount == .2:
                service.dispatch('dog/velocity', {'lease': lease, 'seq': 3, 'vx': -.2, 'vy': -.1, 'yaw': -.3, 'ttl': .5})
        sink.after_send = replace_after_first_axis
        self.clock.advance(.05)
        service._tick()
        nonzero = [p for p in sink.packets if p[0] in (0x0140, 0x0145, 0x0141) and p[1] != 0]
        self.assertEqual([p[1] for p in nonzero], [.2])
        self.clock.advance(.05)
        service._tick()
        self.assertEqual([p[1] for p in sink.packets[-3:]], [-.2, -.1, -.3])

    def test_worker_watchdog_expires_lease_without_any_further_api_calls(self):
        sink = DatagramSink()
        stopped = threading.Event()
        stationary_count = [0]
        def observe(code, amount):
            if code == 0x21010D05:
                stationary_count[0] += 1
                if stationary_count[0] >= 2:
                    stopped.set()
        sink.after_send = observe
        with patch.object(control.socket, 'socket', return_value=sink):
            service = control.ControlService(mode='hardware', dog_host='127.0.0.1')
            self.addCleanup(service.close)
            service.start()
            lease = self.acquire(service)
            service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
            self.assertTrue(stopped.wait(3.0), 'Watchdog did not send stationary after lease expiry')
            self.assertFalse(service.status()['lease_active'])
            service.close()

    def test_bad_actions_modes_and_udp_targets_are_rejected(self):
        self.assert_error(404, 'dog/raw', {})
        self.assert_error(400, 'control/acquire', [])
        disabled = control.ControlService(mode='disabled', clock=self.clock)
        self.addCleanup(disabled.close)
        self.assert_error(503, 'control/acquire', {'owner': 'test'}, disabled)
        for options in ({'mode': 'invalid'}, {'mode': 'hardware', 'dog_host': 'robot.invalid'},
                        {'mode': 'hardware', 'dog_host': 2130706433},
                        {'mode': 'hardware', 'dog_port': True}, {'mode': 'hardware', 'dog_port': 65536}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                control.ControlService(**options)


if __name__ == '__main__':
    unittest.main()
