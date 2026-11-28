"""Control safety checks use a deterministic clock and an in-memory UDP sink."""
import json
import struct
import threading
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


def prepare_dog(service, clock, lease):
    service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
    service._tick()
    for elapsed in (1.5, 1.5, 1.5, .5, .21, .21, .21):
        clock.advance(elapsed)
        service.dispatch('control/heartbeat', {'lease': lease})
        service._tick()


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.service = control.ControlService(clock=self.clock, environment='dev', simulate=True)
        self.addCleanup(self.service.close)

    def acquire(self, service=None):
        return (service or self.service).dispatch('control/acquire', {'owner': 'test'})['lease']

    def assert_error(self, code, action, body, service=None):
        with self.assertRaises(control.ControlError) as caught:
            (service or self.service).dispatch(action, body)
        self.assertEqual(caught.exception.code, code)

    def ready(self, service=None):
        service = service or self.service
        lease = self.acquire(service)
        prepare_dog(service, self.clock, lease)
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
        self.assert_error('conflict', 'control/acquire', {'owner': 'second'})
        self.assertNotIn(lease, json.dumps(self.service.status()))
        self.assert_error('conflict', 'control/heartbeat', {'lease': 'wrong'})
        self.service.dispatch('control/release', {'lease': lease})
        self.assertNotEqual(lease, self.acquire())

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

    def test_stale_scoped_stop_cannot_revoke_a_new_owner(self):
        old = self.acquire()
        self.service.dispatch('control/release', {'lease': old})
        current = self.acquire()
        self.service.dispatch('dog/stand', {'lease': current, 'seq': 1})
        self.assert_error('conflict', 'dog/stop', {'lease': old})
        self.assertTrue(self.service.status()['lease_active'])
        self.assertEqual(self.service.status()['dog']['phase'], 'preparing')
        self.service.dispatch('dog/stop', {'lease': current})
        self.assertFalse(self.service.status()['lease_active'])

    def test_staged_stand_protocol_and_velocity_wire_format(self):
        service, sink = self.hardware()
        lease = self.ready(service)
        stage_codes = [p[0] for p in sink.packets if p[0] not in (0x21040001, 0x0140, 0x0145, 0x0141)]
        self.assertEqual(stage_codes, [0x21010D05, 0x21010C0A, 0x21010D06, 0x21010300, 0x21010C03])
        self.assertIn((0x21010C0A, 1, 0, ('127.0.0.1', 43893)), sink.packets)
        velocity = {'lease': lease, 'seq': 2, 'vx': .2, 'vy': -.1, 'yaw': .3, 'ttl': .5}
        service.dispatch('dog/velocity', velocity)
        self.assert_error('conflict', 'dog/velocity', velocity, service)
        self.clock.advance(.05)
        service._tick()
        self.assertIn((0x0140, .2, 1, ('127.0.0.1', 43893)), sink.packets)
        self.assertIn((0x0145, -.1, 1, ('127.0.0.1', 43893)), sink.packets)
        self.assertIn((0x0141, .3, 1, ('127.0.0.1', 43893)), sink.packets)
        self.assertLessEqual(sink.timeout, .02)
        self.assertEqual(len(sink.threads), 1)

    def test_stop_cancels_all_later_stand_stages_and_attempts_each_packet(self):
        for failed_code in (0x0140, 0x21010C0A):
            with self.subTest(failed_code=hex(failed_code)):
                service, sink = self.hardware()
                lease = self.acquire(service)
                service.dispatch('dog/stand', {'lease': lease, 'seq': 1})
                service._tick()
                service.dispatch('dog/stop', {})
                sink.packets[:] = []
                sink.fail_codes.add(failed_code)
                for _ in range(10):
                    service._tick()
                    self.clock.advance(.051)
                codes = [packet[0] for packet in sink.packets]
                for code in (0x21010C0A, 0x21040001, 0x0140, 0x0145, 0x0141, 0x21010D05):
                    self.assertEqual(codes.count(code), 10, hex(code))
                self.assertTrue(all(p[1] == 0 for p in sink.packets))
                self.assertNotIn(0x21010D06, codes)
                self.assertNotIn(0x21010300, codes)
                self.assertEqual(service.status()['dog']['phase'], 'error')
                self.assertIsNotNone(service.status()['dog']['error'])
                self.assert_error('locked', 'control/acquire', {'owner': 'test'}, service)
                sink.fail_codes.clear()
                service.dispatch('control/reset', {'revision': service.status().get('safety_revision')})
                self.assertFalse(service.status()['lease_active'])
                self.assertIsNone(service.status()['dog']['error'])

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
            self.assertIn((0x21010C0A, 0, 0, ('127.0.0.1', 43893)), sink.packets)
            self.assertFalse(service.status()['lease_active'])
            service.close()

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


if __name__ == '__main__':
    unittest.main()
