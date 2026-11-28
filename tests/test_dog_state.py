"""Measured posture uses fixed vendor offsets and passive loopback transport."""
import errno
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch

from src import dog_state


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def state_packet(basic=6, gait=0, motion=0, need_move=0):
    # Offsets derived independently from the vendor C++ header's native struct.
    payload = bytearray(208)
    struct.pack_into('<iii', payload, 0, basic, gait, 0)
    struct.pack_into('<18d', payload, 16, *([0.0] * 18))
    struct.pack_into('<i', payload, 172, motion)
    struct.pack_into('<d', payload, 176, 0.75)
    payload[188] = need_move
    struct.pack_into('<2d', payload, 192, 1.0, 2.0)
    return struct.pack('<III', 0x0901, 208, 1) + payload


class DogStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.receiver = dog_state.DogStateReceiver('127.0.0.1', clock=self.clock)
        self.addCleanup(self.receiver.close)

    def accept(self, packet=None, host='127.0.0.1'):
        self.receiver._accept_datagram(state_packet() if packet is None else packet,
                                       (host, 50000))
        return self.receiver.snapshot()

    def test_constructor_and_close_do_not_open_transport(self):
        with patch.object(dog_state.socket, 'socket', side_effect=AssertionError('socket opened')):
            receiver = dog_state.DogStateReceiver('127.0.0.1')
            self.assertFalse(receiver.snapshot()['observed'])
            self.assertFalse(receiver.snapshot()['motion_ready'])
            receiver.close()
            self.assertIs(receiver.start(), receiver)
            receiver.close()

    def test_measured_standing_and_freshness_boundary(self):
        state = self.accept()
        self.assertEqual((state['basic_state'], state['gait_state'], state['motion_state']), (6, 0, 0))
        self.assertTrue(state['observed'])
        self.assertFalse(state['simulated'])
        self.assertTrue(state['fresh'])
        self.assertTrue(state['standing'])
        self.assertTrue(state['motion_ready'])
        self.assertIsNone(state['reason'])
        self.clock.now += 0.5
        self.assertTrue(self.receiver.snapshot()['motion_ready'])
        self.clock.now += 0.001
        stale = self.receiver.snapshot()
        self.assertTrue(stale['observed'])
        self.assertFalse(stale['fresh'])
        self.assertFalse(stale['standing'])
        self.assertFalse(stale['motion_ready'])
        self.assertAlmostEqual(stale['age_s'], 0.501)
        self.assertIn('stale', stale['reason'].lower())

    def test_only_stable_low_gait_supports_motion(self):
        for basic, gait, motion, balance, standing, ready in (
                (6, 0, 1, 0, False, True), (6, 0, 0, 1, False, False),
                (1, 0, 0, 0, False, False), (5, 0, 0, 0, False, False),
                (8, 0, 0, 0, False, False), (16, 0, 1, 0, False, False),
                (6, 4, 0, 0, False, False), (6, 0, 2, 0, False, False)):
            with self.subTest(basic=basic, gait=gait, motion=motion, balance=balance):
                state = self.accept(state_packet(basic, gait, motion, balance))
                self.assertTrue(state['observed'])
                self.assertEqual(state['standing'], standing)
                self.assertEqual(state['motion_ready'], ready)
                if not ready:
                    self.assertTrue(state['reason'])

    def test_foreign_source_and_other_channels_cannot_refresh_feedback(self):
        self.accept()
        self.clock.now += 0.6
        self.accept(host='127.0.0.2')
        self.accept(struct.pack('<III', 0x0902, 96, 1) + bytes(96))
        self.assertFalse(self.receiver.snapshot()['fresh'])
        self.assertAlmostEqual(self.receiver.snapshot()['age_s'], 0.6)

    def test_malformed_state_invalidates_previous_sample_and_can_recover(self):
        valid = state_packet()
        malformed = [b'', valid[:3], valid[:11], valid[:-1], valid + b'\0',
                     struct.pack('<III', 0x0901, 200, 1) + valid[12:],
                     struct.pack('<III', 0x0901, 208, 0) + valid[12:],
                     state_packet(need_move=2), state_packet(basic=-1)]
        for packet in malformed:
            with self.subTest(length=len(packet), prefix=packet[:12]):
                self.assertTrue(self.accept()['standing'])
                state = self.accept(packet)
                self.assertFalse(state['observed'])
                self.assertFalse(state['motion_ready'])
                self.assertIsNone(state['age_s'])
                self.assertTrue(state['reason'])
        self.assertTrue(self.accept()['motion_ready'])

    def test_nonfinite_telemetry_doubles_fail_closed(self):
        for offset in tuple(range(16, 160, 8)) + (176, 192, 200):
            for value in (float('nan'), float('inf'), float('-inf')):
                with self.subTest(offset=offset, value=value):
                    self.accept()
                    packet = bytearray(state_packet())
                    struct.pack_into('<d', packet, 12 + offset, value)
                    state = self.accept(packet)
                    self.assertFalse(state['observed'])
                    self.assertFalse(state['motion_ready'])

    def test_snapshot_cannot_mutate_cached_feedback(self):
        state = self.accept()
        state['basic_state'] = 1
        state['standing'] = False
        self.assertTrue(self.receiver.snapshot()['standing'])
        self.assertEqual(self.receiver.snapshot()['basic_state'], 6)

    def test_close_invalidates_sample_and_blocks_late_packets(self):
        self.accept()
        self.receiver.close()
        state = self.accept()
        self.assertFalse(state['observed'])
        self.assertFalse(state['motion_ready'])
        self.assertIn('closed', state['reason'].lower())

    def test_occupied_port_is_reported_and_never_shared(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as occupant:
            occupant.bind(('127.0.0.1', 0))
            receiver = dog_state.DogStateReceiver('127.0.0.1', occupant.getsockname()[1])
            self.addCleanup(receiver.close)
            self.assertIs(receiver.start(), receiver)
            state = receiver.snapshot()
            self.assertFalse(state['motion_ready'])
            self.assertIn('bind', state['reason'].lower())
            self.assertIs(receiver.start(), receiver)

    def test_passive_loopback_receive_and_shutdown(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        receiver = dog_state.DogStateReceiver('127.0.0.1', port)
        self.addCleanup(receiver.close)
        self.assertIs(receiver.start(), receiver)
        self.assertIs(receiver.start(), receiver)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            sender.settimeout(0.1)
            sender.sendto(state_packet(), ('127.0.0.1', port))
            deadline = time.monotonic() + 1.0
            while not receiver.snapshot()['observed'] and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(receiver.snapshot()['standing'])
            with self.assertRaises(socket.timeout):
                sender.recvfrom(1024)
        before = time.monotonic()
        receiver.close()
        self.assertLess(time.monotonic() - before, 1.0)
        self.assertFalse(receiver.snapshot()['observed'])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
            replacement.bind(('127.0.0.1', port))

    def test_receive_failure_invalidates_last_sample(self):
        failed = threading.Event()

        class FailedSocket:
            def __init__(self):
                self.first = True

            def settimeout(self, timeout):
                pass

            def bind(self, address):
                pass

            def recvfrom(self, size):
                if self.first:
                    self.first = False
                    return state_packet(), ('127.0.0.1', 40000)
                failed.set()
                raise OSError('injected receive failure')

            def close(self):
                pass

        with patch.object(dog_state.socket, 'socket', return_value=FailedSocket()):
            receiver = dog_state.DogStateReceiver('127.0.0.1').start()
            self.addCleanup(receiver.close)
            self.assertTrue(failed.wait(1.0))
            receiver._thread.join(1.0)
            state = receiver.snapshot()
            self.assertFalse(state['observed'])
            self.assertFalse(state['motion_ready'])
            self.assertIn('fail', state['reason'].lower())

    def test_thread_start_failure_releases_bound_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        receiver = dog_state.DogStateReceiver('127.0.0.1', port)
        self.addCleanup(receiver.close)
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('thread limit')):
            self.assertIs(receiver.start(), receiver)
        self.assertFalse(receiver.snapshot()['motion_ready'])
        self.assertIn('fail', receiver.snapshot()['reason'].lower())
        receiver.close()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as replacement:
            replacement.bind(('127.0.0.1', port))

    def test_scheduling_stall_discards_queued_posture_then_recovers_from_live_packet(self):
        receiver = self.receiver
        clock = self.clock
        snapshots = []

        class QueuedSocket:
            def __init__(self):
                self.reads = 0
                self.timeout = 0.1
                self.live_timeout = None

            def settimeout(self, value):
                self.timeout = value

            def recvfrom(self, size):
                self.reads += 1
                if self.reads == 1:
                    clock.now += 0.6
                    return state_packet(), ('127.0.0.1', 40000)
                if self.reads <= 131:
                    snapshots.append(receiver.snapshot())
                    return state_packet(), ('127.0.0.1', 40000)
                if self.reads == 132:
                    snapshots.append(receiver.snapshot())
                    raise BlockingIOError(errno.EAGAIN, 'queue is empty')
                if self.reads == 133:
                    self.live_timeout = self.timeout
                    return state_packet(), ('127.0.0.1', 40000)
                receiver._stop.set()
                raise socket.timeout()

            def close(self):
                pass

        transport = QueuedSocket()
        receiver._socket = transport
        receiver._run()
        self.assertTrue(snapshots)
        self.assertTrue(all(not state['motion_ready'] for state in snapshots))
        self.assertTrue(all(not state['observed'] for state in snapshots))
        self.assertTrue(receiver.snapshot()['standing'])
        self.assertEqual(transport.live_timeout, 0.1)

    def test_regular_receive_timeouts_allow_the_next_live_posture(self):
        receiver = self.receiver
        clock = self.clock

        class IdleSocket:
            def __init__(self):
                self.reads = 0

            def settimeout(self, value):
                pass

            def recvfrom(self, size):
                self.reads += 1
                clock.now += 0.1
                if self.reads <= 10:
                    raise socket.timeout()
                if self.reads == 11:
                    return state_packet(), ('127.0.0.1', 40000)
                receiver._stop.set()
                raise socket.timeout()

            def close(self):
                pass

        receiver._socket = IdleSocket()
        receiver._run()
        self.assertTrue(receiver.snapshot()['standing'])

    def test_scheduling_gap_while_emptying_queue_keeps_old_posture_blocked(self):
        receiver = self.receiver
        clock = self.clock
        snapshots = []

        class DelayedEmptySocket:
            def __init__(self):
                self.reads = 0

            def settimeout(self, value):
                pass

            def recvfrom(self, size):
                self.reads += 1
                if self.reads == 1:
                    clock.now += 0.6
                elif self.reads == 2:
                    clock.now += 0.6
                    raise BlockingIOError(errno.EAGAIN, 'empty before scheduling delay')
                elif self.reads == 4:
                    snapshots.append(receiver.snapshot())
                    raise BlockingIOError(errno.EAGAIN, 'queue is empty')
                elif self.reads == 6:
                    receiver._stop.set()
                    raise socket.timeout()
                return state_packet(), ('127.0.0.1', 40000)

            def close(self):
                pass

        receiver._socket = DelayedEmptySocket()
        receiver._run()
        self.assertTrue(snapshots)
        self.assertFalse(snapshots[0]['motion_ready'])
        self.assertTrue(receiver.snapshot()['standing'])

    def test_queue_drain_failure_and_stop_leave_feedback_untrusted(self):
        for fail in (True, False):
            with self.subTest(fail=fail):
                receiver = dog_state.DogStateReceiver('127.0.0.1', clock=self.clock)
                self.addCleanup(receiver.close)
                clock = self.clock

                class InterruptedDrainSocket:
                    def __init__(self):
                        self.reads = 0
                        self.timeout = 0.1

                    def settimeout(self, value):
                        self.timeout = value

                    def recvfrom(self, size):
                        self.reads += 1
                        if self.reads == 1:
                            clock.now += 0.6
                        elif fail:
                            raise OSError('injected queue drain failure')
                        else:
                            receiver._stop.set()
                        return state_packet(), ('127.0.0.1', 40000)

                    def close(self):
                        pass

                transport = InterruptedDrainSocket()
                receiver._socket = transport
                receiver._run()
                self.assertFalse(receiver.snapshot()['observed'])
                self.assertFalse(receiver.snapshot()['motion_ready'])
                self.assertEqual(transport.timeout, 0.1)
                if fail:
                    self.assertIn('fail', receiver.snapshot()['reason'])

    def test_processing_delay_cannot_renew_the_received_posture_timestamp(self):
        receiver = self.receiver
        clock = self.clock

        class OnePacketSocket:
            def __init__(self):
                self.first = True
                self.after_decode_ready = None

            def settimeout(self, value):
                pass

            def recvfrom(self, size):
                if self.first:
                    self.first = False
                    return state_packet(), ('127.0.0.1', 40000)
                self.after_decode_ready = receiver.snapshot()['motion_ready']
                receiver._stop.set()
                raise socket.timeout()

            def close(self):
                pass

        real_decode = dog_state._decode

        def delayed_decode(packet):
            clock.now += 0.6
            return real_decode(packet)

        transport = OnePacketSocket()
        receiver._socket = transport
        with patch.object(dog_state, '_decode', side_effect=delayed_decode):
            receiver._run()
        self.assertFalse(receiver.snapshot()['motion_ready'])
        self.assertFalse(transport.after_decode_ready)

    def test_subthreshold_queue_delay_counts_toward_feedback_age(self):
        receiver = self.receiver
        clock = self.clock
        snapshots = []

        class DelayedPacketSocket:
            def __init__(self):
                self.first = True

            def settimeout(self, value):
                pass

            def recvfrom(self, size):
                if self.first:
                    self.first = False
                    clock.now += 0.4
                    return state_packet(), ('127.0.0.1', 40000)
                clock.now += 0.3
                snapshots.append(receiver.snapshot())
                receiver._stop.set()
                raise BlockingIOError(errno.EAGAIN, 'queue is empty')

            def close(self):
                pass

        receiver._socket = DelayedPacketSocket()
        receiver._run()
        self.assertEqual(len(snapshots), 1)
        self.assertFalse(snapshots[0]['fresh'])
        self.assertFalse(snapshots[0]['motion_ready'])
        self.assertAlmostEqual(snapshots[0]['age_s'], 0.7)

    def test_sustained_backlog_never_renews_its_empty_queue_timestamp(self):
        receiver = self.receiver
        clock = self.clock
        backlog_snapshots = []

        class SustainedBacklogSocket:
            def __init__(self):
                self.reads = 0

            def settimeout(self, value):
                pass

            def recvfrom(self, size):
                self.reads += 1
                if self.reads <= 130:
                    clock.now += 0.01
                    if self.reads >= 60:
                        backlog_snapshots.append(receiver.snapshot())
                    return state_packet(), ('127.0.0.1', 40000)
                if self.reads in (131, 133):
                    raise BlockingIOError(errno.EAGAIN, 'queue is empty')
                if self.reads == 132:
                    return state_packet(), ('127.0.0.1', 40000)
                receiver._stop.set()
                raise socket.timeout()

            def close(self):
                pass

        receiver._socket = SustainedBacklogSocket()
        receiver._run()
        self.assertTrue(backlog_snapshots)
        self.assertTrue(all(not state['motion_ready'] for state in backlog_snapshots))
        self.assertTrue(receiver.snapshot()['standing'])


if __name__ == '__main__':
    unittest.main()
