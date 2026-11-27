"""Check JPEG playback, stream completion, and transfer timeouts in a browser.

Run locally: python3 tests/camera_browser_smoke.py
The test uses a temporary Chrome profile and loopback addresses. It needs no robot or camera.
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.server import PAGE


def find_browser():
    candidates = []
    if sys.platform == "win32":
        for browser in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"):
            for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                if os.environ.get(variable):
                    candidates.append(Path(os.environ[variable]) / browser)
    elif sys.platform == "darwin":
        candidates = [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                      Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                      Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    raise FileNotFoundError("Chrome/Chromium/Edge was not found. Pass --browser with its executable path.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Chrome, Chromium or Edge executable (default: detect installed browser)")
    args = parser.parse_args()
    try:
        browser_path = args.browser or find_browser()
    except FileNotFoundError as error:
        parser.error(str(error))
    observations = queue.Queue()
    requests = []
    live_observations = {}
    stopped_observations = {1: threading.Event(), 2: threading.Event()}
    stream_lock = threading.Lock()
    fixture_errors = []
    frame = (Path(__file__).resolve().parent / "fixtures" / "camera-frame.jpg").read_bytes()
    # Observe the page with its network requests and image decoder unchanged.
    observer = (Path(__file__).resolve().parent / "fixtures" / "browser_observer.html").read_text(encoding="utf-8")
    page = PAGE.replace("__SOURCE_LABEL__", "Camera").replace("<script>", observer + "<script>", 1).encode()
    sources = {1: None, 2: "unknown-source", 3: "realsense", 4: "v4l2"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, content_type, body):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            row = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observations.put(row)
            if row["status"] == "Live" and not row["hidden"] and row["width"] == 160:
                observed = live_observations.get(row["attempt"])
                if observed:
                    observed.set()
            if row["hidden"] and row["attempt"] in stopped_observations:
                reason = "ended" if row["attempt"] == 1 else "timed out"
                if reason in row["status"]:
                    stopped_observations[row["attempt"]].set()
            self.reply("text/plain", b"ok")

        def do_GET(self):
            try:
                if self.path == "/":
                    self.reply("text/html", page)
                elif self.path == "/healthz":
                    self.reply("application/json", b'{"source":"realsense","status":"live","generation":0}')
                elif self.path.startswith("/stream.mjpg"):
                    with stream_lock:
                        requests.append(time.monotonic())
                        attempt = len(requests)
                        observed = live_observations[attempt] = threading.Event()
                    # Keep the failure state observable before serving the next stream.
                    # Headers change the observer's stream identity, so wait before sending them.
                    if attempt in (2, 3) and not stopped_observations[attempt - 1].wait(10):
                        fixture_errors.append("No stopped-frame observation for stream {}".format(attempt - 1))
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("X-Test-Stream", str(attempt))
                    source = sources.get(attempt, "realsense")
                    if source is not None:
                        self.send_header("X-Camera-Source", source)
                    self.end_headers()
                    part = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                    # Split the header across TCP reads to check partial header handling.
                    self.wfile.write(part[:30])
                    time.sleep(0.03)
                    self.wfile.write(part[30:])
                    # Slow CI browsers must observe playback before we trigger EOF or a stall.
                    # Keep delivering real frames while waiting, so this is not a stream timeout.
                    observation_deadline = time.monotonic() + 10
                    while not observed.wait(0.1):
                        if time.monotonic() >= observation_deadline:
                            fixture_errors.append("No decoded-frame observation for stream {}".format(attempt))
                            return
                        self.wfile.write(part)
                    time.sleep(3.5 if attempt == 2 else 0.3)
                    self.wfile.write(b"--frame--\r\n")
                else:
                    self.send_error(404)
            except (BrokenPipeError, ConnectionResetError):
                pass

    rows = []
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory(prefix="lite3-camera-chrome-") as profile:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = subprocess.Popen([browser_path, "--headless", "--disable-gpu", "--disable-background-networking",
                                    "--disable-extensions", "--no-first-run", "--user-data-dir=" + profile,
                                    "http://127.0.0.1:{}/".format(server.server_port)],
                                   stdout=subprocess.DEVNULL, stderr=None)
        try:
            # Browser cold startup is separate from the playback/recovery deadline.
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if browser.poll() is not None:
                    raise AssertionError("Browser exited before completing playback checks: {}".format(browser.returncode))
                try:
                    row = observations.get(timeout=1)
                except queue.Empty:
                    continue
                if not rows:
                    deadline = time.monotonic() + 45
                rows.append(row)
                if row["reloaded"] and row["time"] > 1000 and not row["hidden"]:
                    break
        finally:
            if os.name == "nt" and browser.poll() is None:
                # Chrome's renderer children otherwise retain locks on the temporary profile.
                subprocess.run(["taskkill", "/PID", str(browser.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            elif browser.poll() is None:
                browser.terminate()
            try:
                browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait()
            server.shutdown()
    assert rows, "The browser did not report observations before the startup deadline"
    assert not fixture_errors, "; ".join(fixture_errors)
    assert len(requests) >= 3, "The viewer did not reconnect after stream completion and timeout"
    assert any(row["reloaded"] and row["status"] == "Live" for row in rows), "Playback did not survive a page refresh"
    assert any(row["snapshot"] for row in rows), "The viewer did not expose a snapshot"
    for attempt, label in ((1, "Camera"), (2, "Camera"), (3, "RealSense"), (4, "V4L2 RGB")):
        live = [row for row in rows if row["attempt"] == attempt and row["status"] == "Live"]
        assert live, "The browser did not display stream {}".format(attempt)
        assert all(row["source"] == label and row["alt"] == label + " video stream" for row in live), \
            "The viewer did not identify stream {} as {}".format(attempt, label)
    assert any(row["reloaded"] and row["status"] == "Live" and row["source"] == "RealSense" for row in rows), \
        "The viewer did not restore its source after refresh"
    assert any(not row["hidden"] and row["width"] == 160 and row["status"] == "Live" for row in rows), "The browser did not decode and display a JPEG frame"
    assert any(row["hidden"] and "ended" in row["status"] for row in rows), "The viewer kept the last frame after stream completion"
    assert any(row["hidden"] and "timed out" in row["status"] for row in rows), "The viewer kept the last frame after a transfer timeout"
    print("PASS: Chrome displayed JPEG frames, hid frames after stream completion and timeout, and reconnected ({} streams).".format(len(requests)))


if __name__ == "__main__":
    main()
