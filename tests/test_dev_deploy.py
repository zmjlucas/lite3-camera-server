"""Check control settings passed to the remote launcher."""
import shlex
import unittest
from unittest.mock import patch
from src import client


class DevDeployTests(unittest.TestCase):
    def test_native_console_launch_forwards_remote_configuration(self):
        for mode in ('hardware', 'readonly'):
            with self.subTest(mode=mode):
                args = client.parser().parse_args([
                    'view', 'robot', '--backend', 'none', '--console',
                    '--control-mode', mode, '--dog-host', '192.168.1.120'])
                command = shlex.split(client.real_script(args))
                self.assertIn('--require-dev', command)
                self.assertEqual(command[-4:], [
                    '--control-mode', mode, '--dog-host', '192.168.1.120'])

    def test_ros_cannot_silently_ignore_robot_control_options(self):
        with patch.object(client.subprocess,'call') as run:
            with self.assertRaises(SystemExit):
                client.main(['view','robot','--console','--dry-run'])
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
