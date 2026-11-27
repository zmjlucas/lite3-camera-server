"""Hardware observation must never open the dog's motion transport."""
import unittest
from unittest.mock import patch

from src import control


class ReadOnlyTests(unittest.TestCase):
    def test_full_lifecycle_and_mutations_cannot_open_udp(self):
        with patch.object(control.socket, 'socket', side_effect=AssertionError('UDP forbidden')) as udp:
            service = control.ControlService(mode='readonly')
            service.start()
            try:
                for action, body in (
                    ('control/acquire', {'owner':'test'}), ('control/release', {'lease':'x'}),
                    ('control/estop', {}), ('control/reset', {}), ('dog/stop', {}),
                    ('dog/stand', {'lease':'x','seq':1}),
                    ('dog/velocity', {'lease':'x','seq':2,'vx':.1,'vy':0,'yaw':0,'ttl':.5}),
                    ('arm/jog', {'lease':'x','seq':3,'joint':'e','delta':1,'speed':1})):
                    with self.subTest(action=action), self.assertRaises(control.ControlError):
                        service.dispatch(action, body)
                self.assertEqual(service.status()['mode'],'readonly')
                self.assertFalse(service.status()['arm']['motion_enabled'])
            finally:
                service.close()
            self.assertFalse(service._thread.is_alive())
            udp.assert_not_called()
if __name__ == '__main__':
    unittest.main()
