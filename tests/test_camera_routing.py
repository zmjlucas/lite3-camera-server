"""Check route selection with sample OS routes. These tests do not connect to a robot."""
import contextlib
import importlib.util
import io
from pathlib import Path
import shlex
import subprocess
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/lite3_camera.py"
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
WIFI_DEFAULT = """   route to: 192.168.1.103
destination: default
       mask: default
    gateway: 192.168.2.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""
USB_DEFAULT = """   route to: 192.168.1.103
destination: default
       mask: default
    gateway: 172.20.10.1
  interface: en7
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""
DIRECT_TARGET = """   route to: 192.168.1.103
destination: 192.168.1
       mask: 255.255.255.0
    gateway: link#6
  interface: en0
      flags: <UP,DONE,CLONING,STATIC>
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
        spec = importlib.util.spec_from_file_location("camera_route_cli", SCRIPT)
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)
        self.routes = {"192.168.1.103": USB_DEFAULT, GATEWAY: WIFI_GATEWAY}
        self.route_queries = []
        self.hardware_queries = []
        self.hardware_ports = HARDWARE_PORTS
        self.remote_commands = []

    def route_command(self, command, **kwargs):
        # Reject network probes, writes, commands without time limits, and shell execution.
        self.assertFalse(kwargs.get("shell", False))
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 3)
        if command == ["networksetup", "-listallhardwareports"]:
            self.hardware_queries.append(command)
            if isinstance(self.hardware_ports, Exception):
                raise self.hardware_ports
            return subprocess.CompletedProcess(command, 0, self.hardware_ports, "")
        self.assertEqual(command[:3], ["route", "-n", "get"])
        self.assertEqual(len(command), 4)
        self.route_queries.append(command[-1])
        value = self.routes.get(command[-1])
        if isinstance(value, Exception):
            raise value
        return subprocess.CompletedProcess(command, 0 if value else 1, value or "", "")

    def run_cli(self, *arguments, platform="darwin"):
        stdout, stderr = io.StringIO(), io.StringIO()

        def remote(command, *args):
            self.remote_commands.append(command)
            return 0

        with patch.object(self.cli.sys, "platform", platform), \
                patch.object(self.cli.subprocess, "run", self.route_command), \
                patch.object(self.cli.subprocess, "call", remote), \
                patch.object(self.cli, "run_foreground", remote), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.cli.main(list(arguments))
        self.assertEqual(result, 0, stderr.getvalue())
        return stdout.getvalue(), stderr.getvalue()

    def test_wifi_plus_usb_automatically_uses_robot_jump_for_all_commands(self):
        for command, options, executables in (
                ("doctor", [], ["ssh"]), ("deploy", [], ["ssh", "scp"]),
                ("view", ["--backend", "ros"], ["ssh"]),
                ("view", ["--backend", "realsense"], ["ssh"]),
                ("view", ["--backend", "v4l2", "--device", "/dev/video4"], ["ssh"])):
            with self.subTest(command=command, options=options):
                self.remote_commands.clear()
                self.route_queries.clear()
                _, diagnostic = self.run_cli(command, TARGET, *options)
                self.assertEqual([cmd[0] for cmd in self.remote_commands], executables)
                for cmd in self.remote_commands:
                    self.assertIn("-J", cmd)
                    self.assertEqual(cmd[cmd.index("-J") + 1], "ysc@192.168.2.1")
                    self.assertNotIn("StrictHostKeyChecking=no", cmd)
                self.assertIn("172.20.10.1", diagnostic)
                self.assertIn("en7", diagnostic)
                self.assertIn("192.168.2.1", diagnostic)
                self.assertCountEqual(self.route_queries, ["192.168.1.103", GATEWAY])

    def test_wifi_alone_uses_direct_route_through_robot_gateway(self):
        self.routes["192.168.1.103"] = WIFI_DEFAULT
        _, diagnostic = self.run_cli("doctor", TARGET)
        self.assertNotIn("-J", self.remote_commands[0])
        self.assertIn("direct", diagnostic.lower())
        self.assertIn("192.168.2.1", diagnostic)

    def test_existing_direct_or_static_robot_route_wins_with_usb_present(self):
        for route in (DIRECT_TARGET, WIFI_DEFAULT.replace("destination: default", "destination: 192.168.1")):
            with self.subTest(route=route):
                self.routes["192.168.1.103"] = route
                self.run_cli("view", TARGET)
                self.assertNotIn("-J", self.remote_commands[-1])

    def test_wifi_gateway_link_route_without_arp_entry_is_usable(self):
        self.routes[GATEWAY] = DIRECT_TARGET.replace("192.168.1.103", GATEWAY).replace(
            "destination: 192.168.1", "destination: 192.168.2")
        self.run_cli("doctor", TARGET)
        self.assertIn("-J", self.remote_commands[0])

    def test_overlapping_phone_subnet_does_not_steal_robot_destination(self):
        for route in (DIRECT_TARGET, DIRECT_TARGET.replace("    gateway: link#6\n", "")):
            self.routes["192.168.1.103"] = route.replace("en0", "en7")
            self.run_cli("doctor", TARGET)
            self.assertIn("-J", self.remote_commands[-1])

    def test_gateway_on_usb_is_not_a_robot_wifi_gateway(self):
        self.routes[GATEWAY] = WIFI_GATEWAY.replace("en0", "en7")
        self.run_cli("doctor", TARGET)
        self.assertNotIn("-J", self.remote_commands[0])

    def test_wifi_interface_is_discovered_instead_of_assuming_en0(self):
        self.hardware_ports = HARDWARE_PORTS.replace("en0", "en9")
        self.routes[GATEWAY] = WIFI_GATEWAY.replace("en0", "en9")
        self.routes["192.168.1.103"] = USB_DEFAULT.replace("en7", "en0")
        self.run_cli("doctor", TARGET)
        self.assertIn("-J", self.remote_commands[0])

    def test_missing_hardware_metadata_does_not_guess_a_wifi_interface(self):
        for value in ("AuthorizationCreate() failed: -60008", "", "Device: en0\n",
                      HARDWARE_PORTS.replace("Wi-Fi", "USB Ethernet"),
                      FileNotFoundError("networksetup missing"),
                      subprocess.TimeoutExpired(["networksetup"], 2)):
            with self.subTest(value=value):
                self.hardware_ports = value
                self.run_cli("doctor", TARGET)
                self.assertNotIn("-J", self.remote_commands[-1])

    def test_unavailable_target_route_can_still_use_verified_robot_gateway(self):
        for route in (None, "malformed output", subprocess.TimeoutExpired(["route"], 2)):
            self.routes["192.168.1.103"] = route
            self.run_cli("doctor", TARGET)
            self.assertIn("-J", self.remote_commands[-1])

    def test_gateway_routed_through_usb_is_not_mistaken_for_robot_wifi(self):
        self.routes[GATEWAY] = USB_DEFAULT.replace("192.168.1.103", GATEWAY)
        _, diagnostic = self.run_cli("doctor", TARGET)
        self.assertNotIn("-J", self.remote_commands[0])
        self.assertIn("--jump", diagnostic)

    def test_invalid_gateway_routes_never_trigger_automatic_jump(self):
        for route in (None, "unexpected output", "interface: en0\n",
                      "interface: en0\nflags: <UP>\n",
                      WIFI_GATEWAY.replace("route to: 192.168.2.1", "route to: 192.168.2.103"),
                      WIFI_GATEWAY.replace("destination: 192.168.2.1", "destination:"),
                      WIFI_GATEWAY.replace("UP,", ""),
                      WIFI_GATEWAY.replace("UP,", "UP,REJECT,"),
                      WIFI_GATEWAY.replace("UP,", "UP,BLACKHOLE,"),
                      WIFI_GATEWAY.replace("en0", "lo0"),
                      WIFI_GATEWAY.replace("en0", "utun3")):
            with self.subTest(route=route):
                self.routes[GATEWAY] = route
                self.run_cli("doctor", TARGET)
                self.assertNotIn("-J", self.remote_commands[-1])

    def test_route_tool_failures_preserve_ssh_with_manual_override_hint(self):
        for error in (FileNotFoundError("route missing"), PermissionError("denied"),
                      subprocess.TimeoutExpired(["route"], 2)):
            with self.subTest(error=error):
                self.routes = {"192.168.1.103": error, GATEWAY: error}
                _, diagnostic = self.run_cli("doctor", TARGET)
                self.assertNotIn("-J", self.remote_commands[-1])
                self.assertIn("--jump", diagnostic)

    def test_explicit_jump_and_direct_skip_route_lookup(self):
        for options, expected in ((["--jump", "other@gateway"], "other@gateway"),
                                  (["--direct"], None)):
            for command in ("doctor", "deploy", "view"):
                with self.subTest(options=options, command=command):
                    self.route_queries.clear()
                    self.remote_commands.clear()
                    self.run_cli(command, TARGET, *options)
                    self.assertEqual(self.route_queries, [])
                    self.assertEqual(self.hardware_queries, [])
                    for cmd in self.remote_commands:
                        if expected:
                            self.assertEqual(cmd[cmd.index("-J") + 1], expected)
                        else:
                            self.assertNotIn("-J", cmd)

    def test_direct_and_jump_are_mutually_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            self.cli.parser().parse_args(["doctor", TARGET, "--direct", "--jump", "gateway"])
        self.assertEqual(caught.exception.code, 2)

    def test_auto_jump_preserves_explicit_user_and_unspecified_user(self):
        for target, expected in (("admin@192.168.1.103", "admin@192.168.2.1"),
                                 ("192.168.1.103", "192.168.2.1")):
            self.run_cli("doctor", target)
            cmd = self.remote_commands[-1]
            self.assertIn("-J", cmd)
            self.assertEqual(cmd[cmd.index("-J") + 1], expected)

    def test_dry_run_shows_resolved_jump_without_remote_execution(self):
        stdout, _ = self.run_cli("deploy", TARGET, "--dry-run")
        self.assertEqual(self.remote_commands, [])
        tokens = shlex.split(stdout)
        second_command = tokens.index("scp")
        for cmd in (tokens[:second_command], tokens[second_command:]):
            self.assertIn("-J", cmd)
            self.assertEqual(cmd[cmd.index("-J") + 1], "ysc@192.168.2.1")

    def test_aliases_other_networks_and_platforms_keep_existing_ssh_behavior(self):
        for target, platform in (("robot", "darwin"), ("ysc@robot.local", "darwin"),
                                 ("ysc@10.0.0.2", "darwin"), ("ysc@192.168.2.1", "darwin"),
                                 ("ysc@192.168.1.999", "darwin"), (TARGET, "linux")):
            with self.subTest(target=target, platform=platform):
                self.run_cli("doctor", target, platform=platform)
                self.assertNotIn("-J", self.remote_commands[-1])
                self.assertEqual(self.route_queries, [])
                self.assertEqual(self.hardware_queries, [])

    def test_demo_never_inspects_robot_routes(self):
        self.run_cli("demo", "--dry-run")
        self.assertEqual(self.route_queries, [])
        self.assertEqual(self.hardware_queries, [])
        self.assertEqual(self.remote_commands, [])


if __name__ == "__main__":
    unittest.main()
