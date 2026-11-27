"""The deployed service includes the control runtime and launch settings."""
import unittest
from unittest.mock import patch
from src import client


class DevDeployTests(unittest.TestCase):
    def test_bundle_covers_control_runtime(self):
        assets = {str(path.relative_to(client.SERVER.parent)) for path, _ in client.SERVER_BUNDLE}
        self.assertTrue({'server.py','control.py','arm_gateway.py','remote/arm_worker.py',
                         'web/camera.html','web/control.html'} <= assets)
    def test_native_console_launch_forwards_remote_configuration(self):
        args = client.parser().parse_args(['view','robot','--backend','none','--console',
                                          '--control-mode','hardware',
                                          '--dog-host','192.168.1.120'])
        script = client.real_script(args)
        for option in ('--require-dev','--control-mode','hardware','--dog-host','192.168.1.120'):
            self.assertIn(option,script)
    def test_ros_cannot_silently_ignore_robot_control_options(self):
        with patch.object(client.subprocess,'call') as run:
            with self.assertRaises(SystemExit):
                client.main(['view','robot','--console','--dry-run'])
            self.assertFalse(run.called)
    def test_hardware_deploy_accepts_trusted_lan_configuration(self):
        with patch.object(client, 'deploy', return_value=0):
            self.assertEqual(client.main(['deploy','robot','--control-mode','hardware']), 0)

if __name__ == '__main__':
    unittest.main()
