"""Connection-only viewing uses real HTTP and a local forwarding child; no robots."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src import launcher
from src.camera import LatestFrame
from src.control import ControlService
from src.http_server import CameraHTTPServer
from src.lite3_dev_server import DevClient


FORWARDER = Path(__file__).with_name("fixtures") / "forward_tunnel.py"
PROTOCOLS = [("X-Lite3-Camera-Protocol", "1"), ("X-Lite3-Dev-Protocol", "1")]


class ExistingViewTests(unittest.TestCase):
    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def dev_server(self, frames=False):
        control = ControlService(environment="dev", simulate=True)
        self.addCleanup(control.close)
        latest = LatestFrame("none", stale_after=float("inf"))
        if frames:
            latest.publish(b"frame")
        return self.serve(CameraHTTPServer(0, latest, bind="127.0.0.1", control=control))

    def http_server(self, status=200, headers=PROTOCOLS, stall=False, malformed=False,
                    response_delay=0, chunk_delay=0):
        requests = []
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if stall:
                    release.wait(4)
                    return
                if malformed:
                    self.wfile.write(b"not HTTP\r\n\r\n")
                    return
                if response_delay and release.wait(response_delay):
                    return
                if chunk_delay:
                    try:
                        for chunk in (b"HTTP/1.0 200 OK\r\n", b"X-Lite3-Camera-Protocol: 1\r\n",
                                      b"X-Lite3-Dev-Protocol: 1\r\n", b"X-Test: slow\r\n", b"\r\n"):
                            if release.wait(chunk_delay):
                                return
                            self.wfile.write(chunk)
                    except OSError:
                        pass
                    return
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = self.serve(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        self.addCleanup(release.set)
        return server, requests

    @staticmethod
    def free_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def run_view(self, remote_port, *flags, on_ready=None, delay=0, exit_code=None, interrupt_wait=False):
        local_port = self.free_port()
        url = "http://127.0.0.1:{}/".format(local_port) + ("control" if "--console" in flags else "")
        stdout, stderr = io.StringIO(), io.StringIO()
        children, opened = [], []
        popen, sleep = subprocess.Popen, time.sleep
        interrupted = False
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM))}

        def start(command, **kwargs):
            if command == ["ssh", "-V"]:
                return popen([sys.executable, "-c", "print('OpenSSH_9.8p1')"], **kwargs)
            self.assertEqual(command[0], "ssh")
            self.assertEqual(command[-1], "robot", "Existing-only sent a remote command")
            self.assertIn("-N", command)
            self.assertIn("-T", command)
            self.assertEqual(command[command.index("-L") + 1],
                             "127.0.0.1:{}:127.0.0.1:{}".format(local_port, remote_port))
            replacement = ([sys.executable, "-B", str(FORWARDER), str(local_port), str(remote_port), str(delay)]
                           if exit_code is None else [sys.executable, "-c", "raise SystemExit({})".format(exit_code)])
            child = popen(replacement, **kwargs)
            children.append(child)
            return child

        def wait(seconds):
            nonlocal interrupted
            if not interrupted and (url in stdout.getvalue() or opened or interrupt_wait):
                interrupted = True
                if interrupt_wait:
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                elif on_ready:
                    on_ready(url)
                raise KeyboardInterrupt
            sleep(seconds)

        def open_browser(value):
            opened.append(value)
            return True

        try:
            with patch.object(launcher.subprocess, "Popen", start), \
                    patch.object(launcher.time, "sleep", wait), \
                    patch.object(launcher.webbrowser, "open", open_browser), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = launcher.main(["view", "robot", "--existing-only", "--direct",
                                        "--local-port", str(local_port), "--remote-port", str(remote_port),
                                        "--wait-timeout", "1", *flags])
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll(), "The launcher leaked its SSH child")
            for signum, handler in handlers.items():
                self.assertEqual(signal.getsignal(signum), handler)
            return result, url, stdout.getvalue(), stderr.getvalue(), opened
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)

    def test_dry_run_builds_only_a_forward_with_selected_ports_and_route(self):
        for route in (["--direct"], ["--jump", "ysc@gateway"]):
            with self.subTest(route=route), contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(launcher.main(["view", "robot", "--existing-only", "--console",
                                               "--local-port", "18081", "--remote-port", "8081",
                                               "--dry-run", *route]), 0)
            # Keep the Unix rendering deterministic on every test host.
            if sys.platform == "win32":
                import json
                command = json.loads(stdout.getvalue().splitlines()[0].split(": ", 1)[1])
            else:
                command = shlex.split(stdout.getvalue().splitlines()[0])
            self.assertEqual(command[-1], "robot")
            self.assertIn("-N", command)
            self.assertIn("-T", command)
            self.assertIn("ExitOnForwardFailure=yes", command)
            self.assertEqual(command[command.index("-L") + 1], "127.0.0.1:18081:127.0.0.1:8081")
            self.assertNotIn("StrictHostKeyChecking=no", command)
            if "--jump" in route:
                self.assertEqual(command[command.index("-J") + 1], "ysc@gateway")
            else:
                self.assertNotIn("-J", command)
            self.assertIn("http://127.0.0.1:18081/control", stdout.getvalue())

    def test_launch_settings_are_rejected_before_routing(self):
        settings = [("--backend", "ros"), ("--backend", "none"), ("--env", "dist"), ("--simulate",),
                    ("--control-mode", "readonly"), ("--dog-host", "192.168.1.120"), ("--dog-state-port", "43898"),
                    ("--arm-host", "arm"), ("--arm-device", "/dev/ttyUSB0"), ("--arm-motion-profile", "/tmp/arm.json"),
                    ("--depth",), ("--serial", "camera"), ("--device", "/dev/video0"),
                    ("--wide-angle-host", "192.168.1.120"), ("--width", "640"), ("--height", "480"),
                    ("--fps", "15"), ("--quality", "70"), ("--topic", "/image"), ("--ros-setup", "/opt/ros/setup.bash")]
        for setting in settings:
            with self.subTest(setting=setting), contextlib.redirect_stderr(io.StringIO()) as stderr, \
                    patch.object(launcher, "resolve_jump", side_effect=AssertionError("routing before validation")):
                with self.assertRaises(SystemExit) as caught:
                    launcher.main(["view", "robot", "--existing-only", *setting])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("--existing-only", stderr.getvalue())
                self.assertIn(setting[0], stderr.getvalue())

    def test_existing_only_options_require_the_mode_and_bounded_timeout(self):
        cases = [(["--url-only"], "requires --existing-only"),
                 (["--wait-timeout", "2"], "requires --existing-only")]
        cases.extend((["--existing-only", "--wait-timeout", value], "--wait-timeout")
                     for value in ("0", "-1", "301", "nan", "inf"))
        for flags, diagnostic in cases:
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as caught:
                    launcher.main(["view", "robot", "--direct", *flags])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn(diagnostic, stderr.getvalue())

    def test_url_only_dry_run_reserves_stdout_for_url(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = launcher.main(["view", "robot", "--existing-only", "--url-only", "--direct", "--dry-run"])
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "http://127.0.0.1:18080/\n")
        self.assertIn("ssh", stderr.getvalue())

    def test_url_only_reaches_simulated_sdk_and_preserves_server_on_disconnect(self):
        server = self.dev_server()
        before = server.application.status()

        def sdk_read(url):
            with DevClient(url) as robot:
                self.assertEqual(robot.status()["environment"], "dev")
                self.assertTrue(robot.status()["dog"]["simulated"])

        # A configured proxy must never receive the loopback readiness request.
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
                                     "no_proxy": "", "NO_PROXY": ""}):
            result, url, stdout, _, opened = self.run_view(server.server_port, "--url-only", on_ready=sdk_read)
        self.assertEqual(result, 130)
        self.assertEqual(stdout, url + "\n")
        self.assertEqual(opened, [])
        with DevClient("http://127.0.0.1:{}".format(server.server_port)) as robot:
            self.assertEqual(robot.status(), before)

    def test_console_opens_once_after_compatible_health_with_or_without_frames(self):
        for frames in (False, True):
            with self.subTest(frames=frames):
                server = self.dev_server(frames)
                result, url, stdout, _, opened = self.run_view(server.server_port, "--console", delay=.15)
                self.assertEqual(result, 130)
                self.assertTrue(url.endswith("/control"))
                self.assertEqual(opened, [url])
                self.assertIn("Viewer: " + url, stdout)

    def test_no_open_still_checks_compatibility(self):
        server, requests = self.http_server()
        result, url, stdout, _, opened = self.run_view(server.server_port, "--no-open")
        self.assertEqual(result, 130)
        self.assertEqual(opened, [])
        self.assertEqual(requests, ["/healthz"])
        self.assertIn(url, stdout)

    def test_incompatible_protocols_and_redirects_fail_without_url_or_browser(self):
        destination, redirected = self.http_server()
        cases = [(200, []), (200, PROTOCOLS[:1]), (200, PROTOCOLS[1:]),
                 (200, [("X-Lite3-Camera-Protocol", "1"), ("X-Lite3-Dev-Protocol", "2")]),
                 (200, PROTOCOLS + [("X-Lite3-Dev-Protocol", "1")]), (404, PROTOCOLS),
                 (302, PROTOCOLS + [("Location", "http://127.0.0.1:{}/healthz".format(destination.server_port))])]
        for status, headers in cases:
            with self.subTest(status=status, headers=headers):
                server, requests = self.http_server(status, headers)
                result, _, stdout, stderr, opened = self.run_view(server.server_port, "--url-only")
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(opened, [])
                self.assertIn("protocol", stderr.lower())
                self.assertEqual(requests, ["/healthz"])
        self.assertEqual(redirected, [])

    def test_missing_stalled_and_malformed_service_fail_with_bounded_wait(self):
        stalled, _ = self.http_server(stall=True)
        malformed, _ = self.http_server(malformed=True)
        for port in (self.free_port(), stalled.server_port, malformed.server_port):
            with self.subTest(port=port):
                start = time.monotonic()
                result, _, stdout, stderr, opened = self.run_view(port, "--no-open")
                self.assertEqual(result, 1)
                self.assertLess(time.monotonic() - start, 3)
                self.assertEqual(stdout, "")
                self.assertEqual(opened, [])
                self.assertIn("--remote-port", stderr)

    def test_ssh_exit_before_verification_is_failure_even_with_zero_status(self):
        for code in (0, 42):
            with self.subTest(code=code):
                result, _, stdout, stderr, opened = self.run_view(self.free_port(), "--url-only", exit_code=code)
                self.assertNotEqual(result, 0)
                self.assertEqual(stdout, "")
                self.assertEqual(opened, [])
                self.assertIn("SSH", stderr)
                self.assertIn(str(code), stderr)

    def test_interrupt_during_readiness_reaps_tunnel_without_publishing_url(self):
        result, _, stdout, _, opened = self.run_view(self.free_port(), "--url-only", interrupt_wait=True)
        self.assertEqual(result, 130)
        self.assertEqual(stdout, "")
        self.assertEqual(opened, [])

    def test_occupied_local_port_fails_before_starting_ssh(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess(["ssh", "-V"], 0, "OpenSSH_9.8p1")), \
                    patch.object(launcher.subprocess, "Popen", side_effect=AssertionError("SSH tunnel started with occupied port")), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
                result = launcher.main(["view", "robot", "--existing-only", "--url-only", "--direct",
                                        "--local-port", str(listener.getsockname()[1])])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("--local-port", stderr.getvalue())

    def test_slow_http_response_within_deadline_is_accepted(self):
        server, _ = self.http_server(response_delay=.55)
        result, url, stdout, _, _ = self.run_view(server.server_port, "--url-only")
        self.assertEqual(result, 130)
        self.assertEqual(stdout, url + "\n")

    def test_gradual_http_response_cannot_extend_readiness_deadline(self):
        server, _ = self.http_server(chunk_delay=.25)
        start = time.monotonic()
        result, _, stdout, stderr, opened = self.run_view(server.server_port, "--url-only")
        self.assertEqual(result, 1)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(stdout, "")
        self.assertEqual(opened, [])
        self.assertIn("--wait-timeout", stderr)

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH is required for local configuration inspection")
    def test_ssh_config_cannot_background_or_share_the_forward(self):
        commands = []
        with patch.object(launcher, "show_command", commands.append), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(["view", "robot", "--existing-only", "--direct", "--dry-run"]), 0)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "ssh_config"
            config.write_text("Host *\n  HostName 127.0.0.1\n  ControlMaster auto\n"
                              "  ControlPath persistent-control\n  ControlPersist yes\n"
                              "  IgnoreUnknown TestClientExtension,ForkAfterAuthentication\n"
                              "  TestClientExtension yes\n  ForkAfterAuthentication yes\n",
                              encoding="utf-8")
            command = commands[0]
            inspected = subprocess.run([command[0], "-G", "-F", str(config), *command[1:]],
                                       capture_output=True, text=True, timeout=5)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        settings = dict(line.split(" ", 1) for line in inspected.stdout.splitlines() if " " in line)
        self.assertEqual(settings["controlmaster"], "false")
        self.assertEqual(settings.get("controlpath", "none"), "none")
        self.assertEqual(settings["controlpersist"], "no")
        if "forkafterauthentication" in settings:  # This setting was added in OpenSSH 8.7.
            self.assertEqual(settings["forkafterauthentication"], "no")

    def test_legacy_openssh_does_not_receive_newer_fork_setting(self):
        for version, fork_option in (("OpenSSH_for_Windows_8.1p1, LibreSSL 2.9.2", False),
                                     ("OpenSSH_8.6p1, LibreSSL 3.3.6", False),
                                     ("OpenSSH_8.7p1, OpenSSL", True), ("OpenSSH_10.0p1", True)):
            commands = []
            with self.subTest(version=version), \
                    patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess(["ssh", "-V"], 0, version)), \
                    patch.object(launcher, "show_command", commands.append), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(["view", "robot", "--existing-only", "--direct", "--dry-run"]), 0)
            self.assertEqual("ForkAfterAuthentication=no" in commands[0], fork_option)
            self.assertFalse(any(option.startswith("IgnoreUnknown=") for option in commands[0]))


if __name__ == "__main__":
    unittest.main()
