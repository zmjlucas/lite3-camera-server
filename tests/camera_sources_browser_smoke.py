"""Exercise camera discovery and switching in Chrome with real JPEG decoding.

Run: python3 tests/camera_sources_browser_smoke.py [--browser executable]
Uses a temporary browser profile and loopback fixture; no camera or extra packages.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import tempfile
import threading
import time

from camera_browser_smoke import find_browser
from src.server import PAGE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Chrome, Chromium or Edge executable")
    args = parser.parse_args()
    browser_path = args.browser or find_browser()
    token = secrets.token_hex(32)
    observations = queue.Queue()
    rejected = []
    fixture_errors = []
    switches = []
    state = {"selected": "rgb", "catalog_calls": 0, "rgb_failed": False, "only_arm": False, "capture_failed": False}
    switch_observed = threading.Event()
    arm_selected = threading.Event()
    stopped = threading.Event()
    lock = threading.Lock()
    fixtures = Path(__file__).resolve().parent / "fixtures"
    frame = (fixtures / "camera-frame.jpg").read_bytes()
    observer = (fixtures / "camera_sources_observer.html").read_text(encoding="utf-8")
    page = PAGE.replace("__SOURCE_LABEL__", "Camera").replace("<script>", observer + "<script>", 1).encode()
    sources = [
        {"id": "rgb", "label": "/dev/video4 — D435i RGB", "device": "/dev/video4", "backend": "v4l2"},
        {"id": "arm", "label": "/dev/video6 — Arm camera (SXW USB Camera)", "device": "/dev/video6", "backend": "v4l2"},
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def catalog(self):
            available = sources[1:] if state["only_arm"] else sources
            self.reply(200, json.dumps({"sources": available, "selected": state["selected"]}).encode())

        def authorized(self):
            if self.headers.get("Authorization") == "Bearer " + token:
                return True
            rejected.append(self.path)
            self.reply(401, b'{"error":"Unauthorized"}')
            return False

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/remote-switch":
                    state["selected"] = "arm"
                    self.reply(200, b"{}")
                    return
                if self.path == "/fail-current":
                    state["only_arm"] = True
                    state["capture_failed"] = True
                    self.reply(200, b"{}")
                    return
                if self.path == "/observe":
                    observations.put(body)
                    if body.get("immediate"):
                        switch_observed.set()
                    self.reply(200, b"{}")
                    return
                if self.path != "/source":
                    self.send_error(404)
                    return
                if not self.authorized():
                    return
                if self.headers.get("Origin") != "http://127.0.0.1:{}".format(self.server.server_port):
                    fixture_errors.append("Selection was not same-origin")
                switches.append(body.get("id"))
                if not switch_observed.wait(5):
                    fixture_errors.append("Selection state was not observed")
                switch_observed.clear()
                # Keep sending the old stream while the selection is pending.
                time.sleep(0.3)
                if body["id"] == "rgb" and not state["rgb_failed"]:
                    state["rgb_failed"] = True
                    self.reply(409, b'{"error":"Camera is busy"}')
                    return
                state["selected"] = body["id"]
                state["capture_failed"] = False
                if body["id"] == "arm":
                    arm_selected.set()
                self.catalog()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            try:
                if self.path == "/":
                    self.reply(200, page, "text/html")
                    return
                if self.path == "/favicon.ico":
                    self.reply(204, b"")
                    return
                if not self.authorized():
                    return
                if self.path == "/sources":
                    with lock:
                        state["catalog_calls"] += 1
                        call = state["catalog_calls"]
                    if call == 1:
                        self.reply(503, b'{"error":"Discovery unavailable"}')
                    elif call == 3:
                        stale = json.dumps({"sources": sources, "selected": state["selected"]}).encode()
                        if not arm_selected.wait(5):
                            fixture_errors.append("No selection during delayed catalog refresh")
                        self.reply(200, stale)
                    else:
                        self.catalog()
                elif self.path == "/stream.mjpg":
                    if state["capture_failed"]:
                        self.reply(503, b'{"error":"Camera disconnected"}')
                        return
                    source = state["selected"]
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("X-Camera-Source", "v4l2")
                    self.send_header("X-Camera-Source-Id", source)
                    self.end_headers()
                    part = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
                    while not stopped.is_set() and state["selected"] == source and not state["capture_failed"]:
                        self.wfile.write(part)
                        self.wfile.flush()
                        stopped.wait(0.1)
                else:
                    self.send_error(404)
            except (BrokenPipeError, ConnectionResetError):
                pass

    rows = []
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server, tempfile.TemporaryDirectory(prefix="lite3-sources-chrome-") as profile:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        browser = subprocess.Popen([browser_path, "--headless", "--disable-gpu", "--disable-background-networking",
                                    "--disable-extensions", "--no-first-run", "--user-data-dir=" + profile,
                                    "http://127.0.0.1:{}/#token={}".format(server.server_port, token)],
                                   stdout=subprocess.DEVNULL, stderr=None)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if browser.poll() is not None:
                    raise AssertionError("Browser exited before checking camera selection")
                try:
                    row = observations.get(timeout=1)
                except queue.Empty:
                    continue
                rows.append(row)
                assert row["hasSelector"], "The viewer has no camera source dropdown"
                if row["phase"] == "retry-ready" and row["hidden"] and len(row["options"]) == 1 and "disconnected" in row["status"]:
                    assert row["retryVisible"] and not row["retryDisabled"], "No way to retry the only selected camera after capture failure"
                if len(rows) == 1:
                    deadline = time.monotonic() + 20
                if row.get("complete"):
                    break
        finally:
            stopped.set()
            if os.name == "nt" and browser.poll() is None:
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
    assert rows and rows[-1].get("complete"), "Browser did not finish camera selection: {}".format(rows[-1:] or "no observations")
    assert not fixture_errors, "; ".join(fixture_errors)
    assert not rejected, "Viewer sent unauthenticated requests: {}".format(rejected)
    assert switches == ["arm", "rgb", "rgb", "arm"], switches
    assert all(not row["hash"] for row in rows), "Viewer exposed the session fragment"
    assert any(row["phase"] == "discovery-error" and row["status"] == "Live" and row["source"] == "V4L2 RGB"
               and row["disabled"] and row["sourceStatus"] for row in rows), "Discovery failure prevented playback"
    for phase in ("switch-arm", "failed-switch", "switch-rgb", "retry-current"):
        immediate = next(row for row in rows if row["phase"] == phase and row.get("immediate"))
        assert immediate["disabled"] and immediate["hidden"] and immediate["src"] is None, immediate
        assert immediate["snapshotHidden"] and immediate["snapshot"] is None, immediate
        assert immediate["aborted"] > 0, "Old fetch was not aborted before switching"
        pending = [row for row in rows if row["phase"] == phase and row["disabled"]]
        assert all(row["hidden"] and row["src"] is None and row["snapshotHidden"] and row["snapshot"] is None
                   for row in pending), "Old frames reappeared during {}".format(phase)
    assert any(row["abortedCatalogs"] for row in rows), "Concurrent catalog refresh was not cancelled"
    for phase, identifier, label in (("switch-arm", "arm", sources[1]["label"]),
                                     ("failed-switch", "arm", sources[1]["label"]),
                                     ("switch-rgb", "rgb", sources[0]["label"]),
                                     ("reloaded", "rgb", sources[0]["label"]),
                                     ("retry-current", "arm", sources[1]["label"])):
        live = [row for row in rows if row["phase"] == phase and row["status"] == "Live" and not row["hidden"]]
        assert live, "No decoded playback after {}".format(phase)
        assert all(row["selected"] == identifier and row["source"] == label and row["alt"] == label + " video stream"
                   and row["width"] == 160 and row["snapshot"].startswith("blob:") for row in live), live
    assert any(row["phase"] == "failed-switch" and "busy" in row["sourceStatus"] for row in rows), "Selection failure was not explained"
    remote = [row for row in rows if row["phase"] == "remote-switch" and row["status"] == "Live" and row["streamSource"] == "arm"]
    assert remote and all(row["selected"] == "arm" and row["source"] == sources[1]["label"] for row in remote), \
        "Other-tab selection mislabeled two streams with the same backend"
    assert all(not row["retryVisible"] for row in rows if row["status"] == "Live"), "Retry remained visible during healthy playback"
    assert rows[-1]["options"] == [{"id": "arm", "label": sources[1]["label"]}], "Recovery did not exercise a single-source catalog"
    print("PASS: Chrome recovered discovery, cancelled stale refresh/video, switched cameras, recovered selection failure, preserved authentication, followed another tab's selection, and retried the only camera after failure.")


if __name__ == "__main__":
    main()
