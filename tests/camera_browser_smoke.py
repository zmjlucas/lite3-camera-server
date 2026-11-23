"""Check JPEG playback, stream completion, and transfer timeouts in a browser.

Run locally: python3 tests/camera_browser_smoke.py
The test uses a temporary Chrome profile and loopback addresses. It needs no robot or camera.
"""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.lite3_camera_server import PAGE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    args = parser.parse_args()
    observations = queue.Queue()
    requests = []
    frame = (Path(__file__).resolve().parent / "fixtures" / "camera-frame.jpg").read_bytes()
    # Observe the page with its network requests and image decoder unchanged.
    observer = (Path(__file__).resolve().parent / "fixtures" / "browser_observer.html").read_text(encoding="utf-8")
    page = (PAGE.replace("__SOURCE_LABEL__", "RealSense") + observer).encode()

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
            observations.put(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.reply("text/plain", b"ok")

        def do_GET(self):
            try:
                if self.path == "/":
                    self.reply("text/html", page)
                elif self.path == "/healthz":
                    self.reply("application/json", b'{"source":"realsense","status":"live","generation":0}')
                elif self.path.startswith("/stream.mjpg"):
                    requests.append(time.monotonic())
                    attempt = len(requests)
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.end_headers()
                    part = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                    # Split the header across TCP reads to check partial header handling.
                    self.wfile.write(part[:30])
                    time.sleep(0.03)
                    self.wfile.write(part[30:])
                    time.sleep(0.3 if attempt == 1 else 3.5)
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
                                    "http://127.0.0.1:{}/".format(server.server_port)],
                                   stdout=subprocess.DEVNULL, stderr=None)
        try:
            while True:
                row = observations.get(timeout=10)
                rows.append(row)
                if row["time"] > 5500:
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
    assert any(not row["hidden"] and row["width"] == 160 and row["status"] == "Live" for row in rows), "The browser did not decode and display a JPEG frame"
    assert any(row["hidden"] and "ended" in row["status"] for row in rows), "The viewer kept the last frame after stream completion"
    assert any(row["hidden"] and "timed out" in row["status"] for row in rows), "The viewer kept the last frame after a transfer timeout"
    print("PASS: Chrome displayed JPEG frames, hid frames after stream completion and timeout, and reconnected ({} streams).".format(len(requests)))


if __name__ == "__main__":
    main()
