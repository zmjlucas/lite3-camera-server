"""Check measured posture from the vendor datagram format."""
import struct
import unittest

from src import dog_state
from tests.test_control import Clock


def state_packet(basic=6, motion=0, need_move=0):
    # Offsets derived independently from the vendor C++ header's native struct.
    payload = bytearray(208)
    struct.pack_into('<iii', payload, 0, basic, 0, 0)
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

    def accept(self, packet=None):
        self.receiver._accept_datagram(state_packet() if packet is None else packet,
                                       ('127.0.0.1', 50000))
        return self.receiver.snapshot()

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


if __name__ == '__main__':
    unittest.main()
