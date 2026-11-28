"""Client portability regressions; native Windows filesystem checks run in CI."""
import contextlib
import io
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src import launcher as client
from src import server


class ClientPlatformTests(unittest.TestCase):
    @staticmethod
    def viewer_url():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return "http://127.0.0.1:{}/".format(sock.getsockname()[1])

    def test_view_works_when_sighup_is_unavailable(self):
        windows_signals = SimpleNamespace(SIGTERM=signal.SIGTERM, signal=signal.signal)
        with patch.object(client, "signal", windows_signals), contextlib.redirect_stdout(io.StringIO()):
            result = client.run_foreground([sys.executable, "-c", "pass"], self.viewer_url(), False)
        self.assertEqual(result, 0)

    def test_server_can_start_when_sighup_is_unavailable(self):
        windows_signals = SimpleNamespace(SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT,
                                          signal=lambda *args: None)
        # A bind failure must be reported normally, after signal setup succeeds.
        with patch.object(server, "signal", windows_signals), \
                patch.object(server, "CameraHTTPServer", side_effect=OSError("port busy")), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(server.main([]), 1)
        self.assertIn("port busy", output.getvalue())

    def test_handler_setup_failure_reaps_owned_process(self):
        self.check_initialization_failure(client.signal, "signal", ValueError("no main thread"))

    def test_readiness_initialization_failure_reaps_owned_process(self):
        self.check_initialization_failure(client, "build_opener", OSError("initialization failed"))

    def check_initialization_failure(self, owner, attribute, error):
        children = []
        original_popen = subprocess.Popen

        def start(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            children.append(process)
            return process

        try:
            with patch.object(client.subprocess, "Popen", start), \
                    patch.object(owner, attribute, side_effect=error), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(type(error), str(error)):
                client.run_foreground([sys.executable, "-c", "import time; time.sleep(30)"],
                                      self.viewer_url(), False)
            self.assertIsNotNone(children[0].poll(), "Signal setup failure leaked the SSH child")
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_interrupt_reaps_owned_process_without_unix_signals(self):
        children = []
        original_popen = subprocess.Popen
        original_sleep = time.sleep
        interrupted = False

        def interrupt_once(seconds):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_sleep(seconds)

        def start(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child

        windows_signals = SimpleNamespace(SIGTERM=signal.SIGTERM, signal=signal.signal)
        try:
            with patch.object(client, "signal", windows_signals), \
                    patch.object(client.subprocess, "Popen", start), \
                    patch.object(client.time, "sleep", interrupt_once), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = client.run_foreground([sys.executable, "-c", "import time; time.sleep(30)"],
                                              self.viewer_url(), False)
            self.assertEqual(result, 130)
            self.assertIsNotNone(children[0].poll())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_deploy_captures_binary_content_in_unicode_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / "camera 空格.py"
            content = b"\x00\xff\r\n\n"
            path.write_bytes(content)
            self.assertEqual(client.read_deploy_source(path), content)
            # A leaked Windows handle prevents unlinking this file.
            path.unlink()

    def test_deploy_rejects_directory_and_missing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            for path in (Path(directory).resolve(), Path(directory).resolve() / "missing"):
                with self.subTest(path=path), self.assertRaises(OSError):
                    client.read_deploy_source(path)

    def test_deploy_rejects_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "real").mkdir()
            (root / "real/server.py").write_bytes(b"private")
            try:
                (root / "link").symlink_to(root / "real", target_is_directory=True)
            except OSError as error:
                if os.name == "nt" and error.winerror == 1314:
                    self.skipTest("Windows account lacks symlink privilege; junction coverage runs separately")
                raise
            with self.assertRaises(OSError):
                client.read_deploy_source(root / "link/server.py")

    def test_windows_dry_run_displays_exact_arguments_without_shell_quoting(self):
        with patch.object(client.sys, "platform", "win32"), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            client.show_command(["ssh", "robot", "echo '$HOME'; echo \"hi\""])
        prefix = "Command argv (JSON, inspection only): "
        self.assertTrue(output.getvalue().startswith(prefix))
        self.assertEqual(json.loads(output.getvalue()[len(prefix):]),
                         ["ssh", "robot", "echo '$HOME'; echo \"hi\""])

    def test_browser_smoke_finds_windows_installations_with_spaces(self):
        spec = importlib.util.spec_from_file_location("browser_smoke", Path(__file__).with_name("camera_browser_smoke.py"))
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        with tempfile.TemporaryDirectory() as directory:
            chrome = Path(directory) / "Google/Chrome/Application/chrome.exe"
            edge = Path(directory) / "Microsoft/Edge/Application/msedge.exe"
            edge.parent.mkdir(parents=True)
            edge.touch()
            env = {"PROGRAMFILES": directory, "PROGRAMFILES(X86)": "", "LOCALAPPDATA": ""}
            with patch.object(smoke.sys, "platform", "win32"), \
                    patch.dict(os.environ, env), patch.object(smoke.shutil, "which", return_value=None):
                self.assertEqual(smoke.find_browser(), str(edge))
                chrome.parent.mkdir(parents=True)
                chrome.touch()
                self.assertEqual(smoke.find_browser(), str(chrome))

    @unittest.skipUnless(os.name == "nt", "Windows junctions require Windows")
    def test_deploy_rejects_junction_parent_without_leaking_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            real, link = root / "real", root / "junction"
            real.mkdir()
            (real / "server.py").write_bytes(b"private")
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)],
                           check=True, capture_output=True)
            try:
                with self.assertRaises(OSError):
                    client.read_deploy_source(link / "server.py")
            finally:
                link.rmdir()

    @unittest.skipUnless(os.name == "nt", "Windows sharing modes require Windows")
    def test_windows_capture_refuses_ancestor_already_open_for_delete(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory).resolve()
            source = parent / "server.py"
            source.write_bytes(b"original")
            self.assertEqual(client.read_deploy_source(source), b"original")
            # DELETE access, all sharing enabled, OPEN_EXISTING, BACKUP_SEMANTICS.
            # The client's ancestor pin must conflict before it opens the leaf.
            handle = create(str(parent), 0x10000, 7, None, 3, 0x02000000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                with self.assertRaises(OSError) as caught:
                    client.read_deploy_source(source)
                self.assertEqual(caught.exception.winerror, 32)  # Sharing violation.
            finally:
                kernel.CloseHandle(handle)
            self.assertEqual(client.read_deploy_source(source), b"original")

    @unittest.skipUnless(os.name == "nt", "Windows sharing modes require Windows")
    def test_windows_source_and_ancestors_stay_pinned_until_capture(self):
        import msvcrt
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            parent = root / "camera sources"
            parent.mkdir()
            source = parent / "server.py"
            source.write_bytes(b"original")
            original_open = msvcrt.open_osfhandle

            def check_pinned(handle, flags):
                with self.assertRaises(OSError):
                    source.rename(parent / "moved.py")
                with self.assertRaises(OSError):
                    parent.rename(root / "moved")
                with self.assertRaises(OSError):
                    source.write_bytes(b"replacement")
                return original_open(handle, flags)

            with patch.object(msvcrt, "open_osfhandle", check_pinned):
                self.assertEqual(client.read_deploy_source(source), b"original")
            source.unlink()
            parent.rmdir()


if __name__ == "__main__":
    unittest.main()
