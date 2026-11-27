"""Hardware observation must never open the dog's motion transport."""
import unittest
from unittest.mock import patch

from src import control, server, client


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
    def test_server_and_deployer_accept_only_authenticated_readonly_mode(self):
        with self.assertRaises(SystemExit):
            server.parse_args(['--control-mode','readonly'])
        args = server.parse_args(['--source','none','--control-mode','readonly',
                                  '--control-token-file','/tmp/private-token'])
        self.assertEqual(args.control_mode,'readonly')
        args = client.parser().parse_args(['deploy','robot','--control-mode','readonly',
                                          '--control-token-file','/home/ysc/.dev-token'])
        self.assertIn('readonly',client.control_options(args))

if __name__ == '__main__':
    unittest.main()
