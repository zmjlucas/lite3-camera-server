"""Check control settings passed to the remote launcher."""
import contextlib
import io
import shlex
import unittest
from unittest.mock import patch
from src import launcher as client


class DevDeployTests(unittest.TestCase):

    def test_local_arm_profile_path_reaches_native_detached_and_installed_server(self):
        profile = "/etc/lite3/commissioned arm.json"
        for action in ("view", "deploy"):
            with self.subTest(action=action), contextlib.redirect_stdout(io.StringIO()):
                with patch.object(client, "show_command") as printed:
                    self.assertEqual(client.main([action, "robot", "--backend", "none", "--direct", "--dry-run",
                                                  "--env", "dev", "--control-mode", "hardware",
                                                  "--arm-motion-profile", profile]), 0)
                remote = shlex.split(printed.call_args[0][0][-1])
                self.assertEqual(remote[-4:], ["--control-mode", "hardware", "--arm-motion-profile", profile])
                for command in printed.call_args_list:
                    if command[0][0][0] == "scp":
                        self.assertNotIn(profile, command[0][0])


if __name__ == '__main__':
    unittest.main()
