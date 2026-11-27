"""The deployed service must be self-contained, with secrets kept on its host."""
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
                                          '--control-mode','hardware','--control-token-file','/home/ysc/.dev-token',
                                          '--dog-host','192.168.1.120'])
        script = client.real_script(args)
        for option in ('--require-dev','--control-mode','hardware','--control-token-file','/home/ysc/.dev-token'):
            self.assertIn(option,script)
    def test_ros_cannot_silently_ignore_robot_control_options(self):
        with patch.object(client.subprocess,'call') as run:
            with self.assertRaises(SystemExit):
                client.main(['view','robot','--console','--dry-run'])
            self.assertFalse(run.called)
    def test_hardware_requires_remote_token_path_before_connecting(self):
        with patch.object(client.subprocess,'call') as run:
            with self.assertRaises(SystemExit):
                client.main(['deploy','robot','--control-mode','hardware'])
            self.assertFalse(run.called)

if __name__ == '__main__':
    unittest.main()
