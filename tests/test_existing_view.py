"""Connection-only viewing uses real HTTP and a local forwarding child; no robots."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
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

    def http_server(self, status=200, headers=PROTOCOLS):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(status)
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = self.serve(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
        return server, requests

    @staticmethod
    def free_port():
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    def run_view(self, remote_port, on_ready=None, interrupt_wait=False):
        local_port = self.free_port()
        url = "http://127.0.0.1:{}/".format(local_port)
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
            replacement = [sys.executable, "-B", str(FORWARDER), str(local_port), str(remote_port), "0"]
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
                                        "--wait-timeout", "1", "--url-only"])
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

    def test_url_only_reaches_simulated_sdk_and_preserves_server_on_disconnect(self):
        control = ControlService(environment="dev", simulate=True)
        self.addCleanup(control.close)
        server = self.serve(CameraHTTPServer(0, LatestFrame("none"), bind="127.0.0.1", control=control))
        before = server.application.status()

        def sdk_read(url):
            with DevClient(url) as robot:
                self.assertEqual(robot.status()["environment"], "dev")
                self.assertTrue(robot.status()["dog"]["simulated"])

        # A configured proxy must never receive the loopback readiness request.
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
                                     "no_proxy": "", "NO_PROXY": ""}):
            result, url, stdout, _, opened = self.run_view(server.server_port, on_ready=sdk_read)
        self.assertEqual(result, 130)
        self.assertEqual(stdout, url + "\n")
        self.assertEqual(opened, [])
        with DevClient("http://127.0.0.1:{}".format(server.server_port)) as robot:
            self.assertEqual(robot.status(), before)

    def test_incompatible_protocols_and_redirects_fail_without_url_or_browser(self):
        destination, redirected = self.http_server()
        cases = [(200, [("X-Lite3-Camera-Protocol", "1"), ("X-Lite3-Dev-Protocol", "2")]),
                 (302, PROTOCOLS + [("Location", "http://127.0.0.1:{}/healthz".format(destination.server_port))])]
        for status, headers in cases:
            with self.subTest(status=status, headers=headers):
                server, requests = self.http_server(status, headers)
                result, _, stdout, stderr, opened = self.run_view(server.server_port)
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertEqual(opened, [])
                self.assertIn("protocol", stderr.lower())
                self.assertEqual(requests, ["/healthz"])
        self.assertEqual(redirected, [])

    def test_interrupt_during_readiness_reaps_tunnel_without_publishing_url(self):
        result, _, stdout, _, opened = self.run_view(self.free_port(), interrupt_wait=True)
        self.assertEqual(result, 130)
        self.assertEqual(stdout, "")
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()
