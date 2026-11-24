"""Check JPEG playback, stream completion, and transfer timeouts in a browser.

Run locally: python3 tests/camera_browser_smoke.py
The test uses a temporary Chrome profile and loopback addresses. It needs no robot or camera.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.server import PAGE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    args = parser.parse_args()
    observations = queue.Queue()
    requests = []
    rejected = []
    token = secrets.token_hex(32)
    frame = (Path(__file__).resolve().parent / "fixtures" / "camera-frame.jpg").read_bytes()
    # Observe the page with its network requests and image decoder unchanged.
    observer = (Path(__file__).resolve().parent / "fixtures" / "browser_observer.html").read_text(encoding="utf-8")
    # A browser navigation cannot send the fragment token as an Authorization header.
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
            nonlocal token
            if self.path == "/rotate":
                token = secrets.token_hex(32)
                self.reply("application/json", json.dumps({"token": token}).encode())
            else:
                observations.put(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.reply("text/plain", b"ok")

        def do_GET(self):
            try:
                if self.path != "/" and self.headers.get("Authorization") != "Bearer " + token:
                    rejected.append(self.path)
                    self.send_error(401)
                    return
                if self.path == "/":
                    self.reply("text/html", page)
                elif self.path == "/healthz":
                    self.reply("application/json", b'{"source":"realsense","status":"live","generation":0}')
                elif self.path.startswith("/stream.mjpg"):
                    requests.append(time.monotonic())
                    attempt = len(requests)
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
                    time.sleep(3.5 if attempt == 2 else 0.3)
                    self.wfile.write(b"--frame--\r\n")
                else:
                    self.send_error(404)
            except (BrokenPipeError, ConnectionResetError):
                pass

    rows = []
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory(prefix="lite3-camera-chrome-") as profile:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = subprocess.Popen([args.browser, "--headless", "--disable-gpu", "--disable-background-networking",
                                    "--disable-extensions", "--no-first-run", "--user-data-dir=" + profile,
                                    "http://127.0.0.1:{}/#token={}".format(server.server_port, token)],
                                   stdout=subprocess.DEVNULL, stderr=None)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    row = observations.get(timeout=10)
                except queue.Empty:
                    break
                rows.append(row)
                if row["rotated"] and row["time"] > 1000 and not row["hidden"] and not row["hash"]:
                    break
        finally:
            browser.terminate()
            try:
                browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait()
            server.shutdown()
    assert len(requests) >= 3, "The viewer did not reconnect after stream completion and timeout"
    assert all(not row["hash"] for row in rows), "The viewer left its session token in the address bar"
    assert any(row["reloaded"] and row["status"] == "Live" for row in rows), "Authentication did not survive a page refresh"
    assert any(row["snapshot"] for row in rows), "The viewer did not expose an authenticated snapshot"
    for attempt, label in ((1, "Camera"), (2, "Camera"), (3, "RealSense"), (4, "V4L2 RGB")):
        live = [row for row in rows if row["attempt"] == attempt and row["status"] == "Live"]
        assert live, "The browser did not display stream {}".format(attempt)
        assert all(row["source"] == label and row["alt"] == label + " video stream" for row in live), \
            "The viewer did not identify authenticated stream {} as {}".format(attempt, label)
    assert any(row["reloaded"] and row["status"] == "Live" and row["source"] == "RealSense" for row in rows), \
        "The viewer did not restore its authenticated source after refresh"
    assert any(row["rotated"] and row["status"] == "Live" and not row["hash"] for row in rows), \
        "The viewer did not consume a new session link in the same tab"
    assert not any(path.startswith('/stream.mjpg') for path in rejected), "The viewer sent an unauthenticated stream request"
    assert any(not row["hidden"] and row["width"] == 160 and row["status"] == "Live" for row in rows), "The browser did not decode and display a JPEG frame"
    assert any(row["hidden"] and "ended" in row["status"] for row in rows), "The viewer kept the last frame after stream completion"
    assert any(row["hidden"] and "timed out" in row["status"] for row in rows), "The viewer kept the last frame after a transfer timeout"
    print("PASS: Chrome displayed JPEG frames, hid frames after stream completion and timeout, and reconnected ({} streams).".format(len(requests)))


if __name__ == "__main__":
    main()
