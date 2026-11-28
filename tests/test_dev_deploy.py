"""Check control settings passed to the remote launcher."""
import contextlib
import io
import shlex
import unittest
from unittest.mock import patch
from src import client


class DevDeployTests(unittest.TestCase):
    def test_native_view_and_deploy_select_dist_explicitly(self):
        for action in ('view', 'deploy'):
            with self.subTest(action=action), contextlib.redirect_stdout(io.StringIO()):
                with patch.object(client, 'show_command') as printed:
                    result = client.main([action, 'robot', '--backend', 'none', '--direct', '--dry-run'])
                self.assertEqual(result, 0)
                remote = shlex.split(printed.call_args[0][0][-1])
                self.assertEqual(remote[remote.index('--env') + 1], 'dist')
                self.assertNotIn('--simulate', remote)

    def test_dev_simulation_reaches_detached_and_installed_server(self):
        for action in ('view', 'deploy'):
            with self.subTest(action=action), contextlib.redirect_stdout(io.StringIO()):
                with patch.object(client, 'show_command') as printed:
                    result = client.main([action, 'robot', '--backend', 'none', '--direct', '--dry-run',
                                          '--env', 'dev', '--simulate'])
                self.assertEqual(result, 0)
                remote = shlex.split(printed.call_args[0][0][-1])
                self.assertEqual(remote[remote.index('--env') + 1], 'dev')
                self.assertIn('--simulate', remote)
                if action == 'view':
                    self.assertIn('--require-dev', remote)

    def test_simulation_rejects_dist_and_restricted_control_modes_before_routing(self):
        for flags in ([], ['--env', 'dist'], ['--env', 'dev', '--control-mode', 'readonly'],
                      ['--env', 'dev', '--control-mode', 'disabled']):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()) as stderr:
                with patch.object(client, 'resolve_jump', side_effect=AssertionError('routing before validation')):
                    with self.assertRaises(SystemExit) as error:
                        client.main(['view', 'robot', '--backend', 'none', '--simulate', *flags])
                self.assertEqual(error.exception.code, 2)
                self.assertIn('--simulate requires --env dev', stderr.getvalue())

    def test_default_ros_view_still_works(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(client, 'show_command') as printed:
            self.assertEqual(client.main(['view', 'robot', '--direct', '--dry-run']), 0)
        remote = shlex.split(printed.call_args[0][0][-1])
        self.assertNotIn('--env', remote)
        self.assertNotIn('--simulate', remote)

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

    def test_arm_profile_rejects_remote_arm_or_incompatible_control_mode_before_routing(self):
        for flags in ([], ["--env", "dev", "--control-mode", "readonly"],
                      ["--env", "dev", "--simulate"],
                      ["--env", "dev", "--control-mode", "hardware", "--arm-host", "motion-host"]):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()) as stderr:
                with patch.object(client, "resolve_jump", side_effect=AssertionError("routing before validation")):
                    with self.assertRaises(SystemExit) as error:
                        client.main(["view", "robot", "--backend", "none", "--arm-motion-profile", "/etc/arm.json",
                                     *flags])
                self.assertEqual(error.exception.code, 2)
                self.assertIn("--arm-motion-profile requires --env dev", stderr.getvalue())

    def test_dev_arm_profile_uses_default_hardware_mode(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(client, 'show_command') as printed:
            self.assertEqual(client.main(['deploy', 'robot', '--backend', 'none', '--direct', '--dry-run',
                                          '--env', 'dev', '--arm-motion-profile', '/etc/arm.json']), 0)
        remote = shlex.split(printed.call_args[0][0][-1])
        self.assertEqual(remote[remote.index('--env') + 1], 'dev')
        self.assertEqual(remote[-2:], ['--arm-motion-profile', '/etc/arm.json'])


if __name__ == '__main__':
    unittest.main()
