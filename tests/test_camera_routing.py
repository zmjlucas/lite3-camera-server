"""Check route selection with sample OS routes. These tests do not connect to a robot."""
import contextlib
import io
import subprocess
import unittest
from unittest.mock import patch

from src import launcher as client

RUN_LOCAL_COMMAND = subprocess.run

TARGET = "ysc@192.168.1.103"
GATEWAY = "192.168.2.1"

# macOS route output includes a MAC address or link identifier as the gateway for an on-link route.
WIFI_GATEWAY = """   route to: 192.168.2.1
destination: 192.168.2.1
  interface: en0
    gateway: aa:bb:cc:dd:ee:ff
      flags: <UP,HOST,DONE,LLINFO,WASCLONED,IFSCOPE,IFREF>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500      1100
"""
USB_DEFAULT = """   route to: 192.168.1.103
destination: default
       mask: default
    gateway: 172.20.10.1
  interface: en7
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""
HARDWARE_PORTS = """
Hardware Port: iPhone USB
Device: en7
Ethernet Address: 00:11:22:33:44:55

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: aa:bb:cc:dd:ee:ff

Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: 00:11:22:33:44:66
"""


class CameraRoutingTest(unittest.TestCase):
    def setUp(self):
        self.routes = {"192.168.1.103": USB_DEFAULT, GATEWAY: WIFI_GATEWAY}
        self.route_queries = []
        self.remote_commands = []

    def route_command(self, command, **kwargs):
        # Reject network probes, writes, commands without time limits, and shell execution.
        self.assertFalse(kwargs.get("shell", False))
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 3)
        if command == ["ssh", "-V"]:
            return subprocess.CompletedProcess(command, 0, "OpenSSH_9.8p1", "")
        if command[0] == "git":
            return RUN_LOCAL_COMMAND(command, **kwargs)
        if command == ["networksetup", "-listallhardwareports"]:
            return subprocess.CompletedProcess(command, 0, HARDWARE_PORTS, "")
        self.assertEqual(command[:3], ["route", "-n", "get"])
        self.assertEqual(len(command), 4)
        self.route_queries.append(command[-1])
        return subprocess.CompletedProcess(command, 0, self.routes[command[-1]], "")

    def run_cli(self, *arguments):
        stderr = io.StringIO()

        def remote(command, *args, **kwargs):
            self.remote_commands.append(command)
            return 0

        with patch.object(client.sys, "platform", "darwin"), \
                patch.object(client.subprocess, "run", self.route_command), \
                patch.object(client.subprocess, "call", remote), \
                patch.object(client, "run_foreground", remote), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            result = client.main(list(arguments))
        self.assertEqual(result, 0, stderr.getvalue())
        return stderr.getvalue()

    def test_wifi_plus_usb_automatically_uses_robot_jump_for_all_commands(self):
        for command, options, executables in (
                ("doctor", [], ["ssh"]), ("deploy", [], ["ssh"] + ["scp"] * (len(client.SERVER_BUNDLE) + 1) + ["ssh"]),
                ("view", ["--backend", "realsense"], ["ssh"]),
                ("view", ["--existing-only"], ["ssh"])):
            with self.subTest(command=command, options=options):
                self.remote_commands.clear()
                self.route_queries.clear()
                diagnostic = self.run_cli(command, TARGET, *options)
                self.assertEqual([cmd[0] for cmd in self.remote_commands], executables)
                for cmd in self.remote_commands:
                    self.assertIn("-J", cmd)
                    self.assertEqual(cmd[cmd.index("-J") + 1], "ysc@192.168.2.1")
                    self.assertNotIn("StrictHostKeyChecking=no", cmd)
                self.assertIn("172.20.10.1", diagnostic)
                self.assertIn("en7", diagnostic)
                self.assertIn("192.168.2.1", diagnostic)
                self.assertCountEqual(self.route_queries, ["192.168.1.103", GATEWAY])


if __name__ == "__main__":
    unittest.main()
