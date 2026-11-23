#!/usr/bin/env python3
"""Loopback-only Lite3 camera viewer for use over an SSH tunnel."""

import argparse
import base64
import contextlib
import itertools
import json
import re
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn


LOOPBACK = "127.0.0.1"
BOUNDARY = b"--frame\r\n"
SOURCE_LABELS = {"demo": "Synthetic demo", "realsense": "RealSense", "v4l2": "V4L2 RGB"}

# Four 160x120 frames of a moving test pattern. No camera or JPEG encoder is
# needed to play the demo.
DEMO_JPEGS = (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABIMDRANCxIQDhAUExIVGywdGxgYGzYnKSAsQDlEQz85Pj1HUGZXR0thTT0+"
        "WXlaYWltcnNyRVV9hnxvhWZwcm7/2wBDARMUFBsXGzQdHTRuST5Jbm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5u"
        "bm5ubm5ubm5ubm5ubm5ubm5ubm7/wAARCAB4AKADASIAAhEBAxEB/8QAGwABAQEBAQEBAQAAAAAAAAAAAAMEBQYCBwH/"
        "xAAzEAACAQMBBgMFCAMAAAAAAAAAAQIDBBEFEhQhUVKRBhMxIkFxgbEWI0JTYZLR8KHh8f/EABgBAQEBAQEAAAAAAAAA"
        "AAAAAAABAwIE/8QAHBEBAQACAwEBAAAAAAAAAAAAAAECMQMRUSFB/9oADAMBAAIRAxEAPwDzAANGaYAIKAAomACCgAKJ"
        "gAgoACiYAIKAAomACAAAKAAomACCgAKJgAgoACiYAIKAAomACCgAKAAA+IRlOahCLlKTwklltnZo+FNRqwbn5NF5xszn"
        "l/Hgmd/RdMp6PYOtcQW8yX3jTzhZ4RX+Pn8hV1OtOX3eKa5YyZ3K/jLLk604tXwrqEKblGVGo1+GM3l90kcivQqW1aVG"
        "tBwqQeHFnsKepV4yzNqa5NJfQve2tHW9Oa2Uqsc7Enw2JfHl6CZWbTHk7eAB9Vac6NWdKosThJxks+jXqfJ22UABRMAE"
        "FAAUTABBQAFEwAQDTpsYz1O1hOKlGVaCaaymtpGYraVt2u6NfZ2vKnGeznGcPOAPd6xJqNKOeDbb/vzM+m2kby5dOcnG"
        "Ki5PHq/7k16lTVe2jWptSUfaTi8pxfv+hzaFepb1NulJxl6ZMpp47v61apYws5U/LlJqeeEvdj/p/dIk1XnHPBxy+/8A"
        "sy3FzWuZJ1puTXBcMYN2k0Wtqs88fZX68xdG78eV8RxjDXLlRiorMXhLHFxTZzTbrVxG61a5qwxsuWymnlNJYzn9cZMR"
        "rNPXNJgAKoACiYAIKAAomACCgAKAAA7fh/xDGxpK1u03RT9icVxhl8c81xb5/H3ekVtZXq8y3qRlFPDdGSayfngOLi4y"
        "wlfobsrS2Tq1p4hH1dSSUUcbXPEFOVGpZ2OGnmE6mFs7OPSP0z25nmQJj6kwkTAB00UABRMAEFAAUTABBQAFEwAQAABQ"
        "Hd+1l9+Vbftl/I+1l9+Vbftl/JO7457vjzwNO/VemHZjfqvTDsyukgaN8qco9hvlTlHsUYwad+q9MOzG/VemHZkEgaN8"
        "qco9hvlTlHsUYwad+q9MOzG/VemHZkEgaN8qco9hvlTlHsUYwad+q9MOzG/VemHZkEgAUAABMAEFAAUTABBQAFEwAQUA"
        "BRMAEFAAUTABAAAFAAUTABBQAFEwAQUABRMAEFAAUTABBQAFH//Z",
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABIMDRANCxIQDhAUExIVGywdGxgYGzYnKSAsQDlEQz85Pj1HUGZXR0thTT0+"
        "WXlaYWltcnNyRVV9hnxvhWZwcm7/2wBDARMUFBsXGzQdHTRuST5Jbm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5u"
        "bm5ubm5ubm5ubm5ubm5ubm5ubm7/wAARCAB4AKADASIAAhEBAxEB/8QAGwABAQADAQEBAAAAAAAAAAAAAAMEBQYCBwH/"
        "xAA0EAACAQMBBAcGBgMAAAAAAAAAAQIDBBEFEhQhkQYWMVFSU5ITIkFxgdEjQmGx0vCh4fH/xAAYAQEBAQEBAAAAAAAA"
        "AAAAAAAAAQMCBP/EABwRAQEAAwEBAQEAAAAAAAAAAAABAhExUQMhQf/aAAwDAQACEQMRAD8A5gAGjNMAEFAAUTABBQAF"
        "EwAQUABRMAEFAAUTABAAAFAAUTABBQAFEwAQUABRMAEFAAUTABBQAFAAATABBQAFHiEZTmoQi5Sk8JJZbZuaPRTUasG5"
        "+xovONmc8v58Ezf6LplPR7B1riC3mS/EaecLPCK/x9foKup1py/DxTXdjJncr/GWX01xpavRXUIU3KMqNRr8sZvL5pI1"
        "FehUtq0qNaDhUg8OLOwp6lXjLM2pruaS/Yve2tHW9Oa2Uqsc7Enw2JfPu7BMrOpj9NuAB6q050as6VRYnCTjJZ7Gu08n"
        "bZQAFEwAQUABRMAEAAAUABRMydNjGep2sJxUoyrQTTWU1tIxitpW3a7o19na9lOM9nOM4ecEHd6xJqNKOeDbb/v1MfTb"
        "SN5cunOTjFRcnjtf9yZepU1Xto1qbUlH3k4vKcX8f2NbQr1Lept0pOMuzJlOPHe/rK1SxhZyp+zlJqeeEvhj/p+6RJqv"
        "OOeDjl8/9mLcXNa5knWm5NcFwxgztJotbVZ54+6v17xeHb+OV6RxjDXLlRiorMXhLHFxTZrTN1q4jdatc1YY2XLZTTym"
        "ksZz+uMmEazj1ziYACqAAomACCgAKAAAmACCgAKN30f6QxsaStbtN0U/cnFcYZfHPeuLff8AP4dIrayvV7S3qRlFPDdG"
        "SayfPAcXFxlhK+huytLZOrWniEe11JJRRptc6QU5UalnY4aeYTqYWzs47I/tnl3nMgTH1JhImADpooACiYAIKAAomACA"
        "AAKA3vWy+8q29MvuOtl95Vt6Zfcm7453fHPA33W+/wDJtvTL+Q633/k23pl/Ibvi7vjSg3vWy+8q29MvuOtl95Vt6Zfc"
        "bvibvjngZO/VfDDkxv1Xww5MrpIGRvlTujyG+VO6PIowwZO/VfDDkxv1Xww5MgkDI3yp3R5DfKndHkUYYMnfqvhhyY36"
        "r4YcmQSABQAAEwAQUABRMAEFAAUTABBQAFEwAQUABRMAEAAAUABRMAEFAAUTABBQAFEwAQUABRMAEFAAUf/Z",
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABIMDRANCxIQDhAUExIVGywdGxgYGzYnKSAsQDlEQz85Pj1HUGZXR0thTT0+"
        "WXlaYWltcnNyRVV9hnxvhWZwcm7/2wBDARMUFBsXGzQdHTRuST5Jbm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5u"
        "bm5ubm5ubm5ubm5ubm5ubm5ubm7/wAARCAB4AKADASIAAhEBAxEB/8QAGwABAQADAQEBAAAAAAAAAAAAAAMEBQYCBwH/"
        "xAA0EAACAQMBAwkHBAMAAAAAAAAAAQIDBBEFEhYhBhMUMVFTkZLRIkFScYGx0iNCYfCh4fH/xAAYAQEBAQEBAAAAAAAA"
        "AAAAAAAAAQMCBP/EABwRAQEAAwEBAQEAAAAAAAAAAAABAhFRMQMhQf/aAAwDAQACEQMRAD8A5gAGjNMAEFAAUTABBQAF"
        "EwAQUABRMAEFAAUTABAAAFAAUTABBQAFEwAQUABRMAEFAAUTABBQAFAAATABBQAFEwAQUABR4hGU5qEIuUpPCSWW2bmj"
        "yU1GrBufM0XnGzOeX8+CZv8ARdMp6PYOtcQXSZL9Rp5ws8Ir/H1+gq6nWnL9PFNdmMmdyv8AGWX0140tXkrqEKblGVGo"
        "1+2M3l+KSNRXoVLatKjWg4VIPDizsKepV4yzNqa7GkvsXvbWjrenNbKVWOdiT4bEvn2dQmVnqY/TbgAeqtOdGrOlUWJw"
        "k4yWeprrPJ22UABRMAEAAAUABRMAEFAAUTMnTYxnqdrCcVKMq0E01lNbSMYraVujXdGvs7XNTjPZzjOHnBB3esSajSjn"
        "g22/79TH020jeXLpzk4xUXJ463/cmXqVNV7aNam1JR9pOLynF+/7GtoV6lvU26UnGXVkynjx339ZWqWMLOVPm5SannhL"
        "3Y/6fukSarzjng45fj/sxbi5rXMk603JrguGMGdpNFrarPPH2V/PaL4e38cryjjGGuXKjFRWYvCWOLimzWmbrVxG61a5"
        "qwxsuWymnlNJYzn+cZMI1nj1zxMABVAAUAABMAEFAAUTABBQAFG75P8AKGNjSVrdpuin7E4rjDL457Vxb7fn7ukVtZXq"
        "5y3qRlFPDdGSayfPAcXFxlhK+huytLZOrWniEet1JJRRptc5QU5UalnY4aeYTqYWzs46o/bPh2nMgTHqTCRMAHTRQAFE"
        "wAQAABQG93svu6tvLL1G9l93Vt5ZepN3jnd454G+3vv+5tvLL8hvff8Ac23ll+Q3eLu8aUG93svu6tvLL1G9l93Vt5Ze"
        "o3eJu8c8Dfb33/c23ll+Q3vv+5tvLL8hu8Xd40oN7vZfd1beWXqN7L7urbyy9Ru8Td454GT06r8MPBjp1X4YeDK6SBkd"
        "MqdkfAdMqdkfAowwZPTqvww8GOnVfhh4MgkACgAAJgAgoACiYAIKAAomACCgAKJgAgoACiYAIAAAoACiYAIKAAomACCg"
        "AKJgAgoACiYAIKAAo//Z",
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABIMDRANCxIQDhAUExIVGywdGxgYGzYnKSAsQDlEQz85Pj1HUGZXR0thTT0+"
        "WXlaYWltcnNyRVV9hnxvhWZwcm7/2wBDARMUFBsXGzQdHTRuST5Jbm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubm5u"
        "bm5ubm5ubm5ubm5ubm5ubm5ubm7/wAARCAB4AKADASIAAhEBAxEB/8QAGwABAQEBAQEBAQAAAAAAAAAAAAMFBgQCBwH/"
        "xAA0EAACAQMBBQQIBgMAAAAAAAAAAQIDBBEFEhYhU5IGMVHRExQiQXGBkdIjQlJhsfCh4fH/xAAYAQEBAQEBAAAAAAAA"
        "AAAAAAAAAQMCBP/EABwRAQEAAwEBAQEAAAAAAAAAAAABAhFRMQMhQf/aAAwDAQACEQMRAD8A5gAGjNMAEFAAUTABBQAF"
        "EwAQUABRMAEFAAUTABAAAFAAUTABBQAFEwAQUABRMAEFAAUTABBQAFAAATABBQAFEwAQUABRMAEFAAUfEIynNQhFylJ4"
        "SSy2zZo9lNRqwbn6Gi842Zzy/jwTN/RdMp6PYOtcQXrMl+I084WeEV/j5/IVdTrTl+HimvDGTO5X+Msvprxi1eyuoQpu"
        "UZUajX5YzeX9UkZFehUtq0qNaDhUg8OLOwp6lXjLM2prwaS/gve2tHW9Oa2Uqsc7Enw2JfHw7hMrPUx+m3AA+qtOdGrO"
        "lUWJwk4yWe5rvPk7bAAAoACiYAIKAAomACCgAKJnp02MZ6nawnFSjKtBNNZTW0jzFbSt6td0a+zteinGeznGcPOCDu9Y"
        "k1GlHPBtt/35nn020jeXLpzk4xUXJ473/cnr1Kmq9tGtTako+0nF5Ti/f/Bm0K9S3qbdKTjLuyZTx477+vVqljCzlT9H"
        "KTU88Je7H/T+6RJqvOOeDjl/X/Z5bi5rXMk603JrguGMHu0mi1tVnnj7K/fxF8Pb+OV7RxjDXLlRiorMXhLHFxTZmnt1"
        "q4jdatc1YY2XLZTTymksZz++MniNZ49c8AAVUwAQUABRMAEFAAUTABBQAFG32f7QxsaStbtN0U/YnFcYZfHPiuLfj8fd"
        "0itrK9XpLepGUU8N0ZJrJ+eA4uLjLCV+huytLZOrWniEe91JJRRja52gpyo1LOxw08wnUwtnZx3R/jP08TmQJj1JhImA"
        "DpoAACgN3ey+5Vt0y8xvZfcq26ZeZN3jnd454G9vff8AJtumX3De+/5Nt0y+4bvF3eMUG7vZfcq26ZeY3svuVbdMvMbv"
        "E3eOeBvb33/JtumX3De+/wCTbdMvuG7xd3jFBu72X3KtumXmN7L7lW3TLzG7xN3jngb299/ybbpl9w3vv+TbdMvuG7xd"
        "3jFBu72X3KtumXmN7L7lW3TLzG7xN3jngen16r+mH0Y9eq/ph9GV0kACgAAJgAgoACiYAIKAAomACCgAKJgAgoACiYAI"
        "AAAoACiYAIKAAomACCgAKJgAgoACiYAIKAAo/9k=",
)

PAGE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lite3 camera</title>
<style>
  :root { color-scheme: dark; font-family: system-ui, sans-serif; }
  body { margin: 0; color: #e7eef3; background: #101822; }
  main { max-width: 880px; margin: 0 auto; padding: 2rem 1rem; }
  header { display: flex; flex-wrap: wrap; justify-content: space-between;
           align-items: center; gap: .75rem; margin-bottom: 1rem; }
  h1 { margin: 0; font-size: 1.75rem; }
  .source { color: #a8e9c5; background: #1b342f; padding: .35rem .65rem;
            border-radius: .35rem; }
  figure { margin: 0; display: grid; place-items: center; min-height: 15rem;
           background: #192633; border: 1px solid #364759; border-radius: .6rem;
           overflow: hidden; }
  img { width: 100%; max-height: 75vh; object-fit: contain; }
  #placeholder { color: #a6b8c7; padding: 1rem; text-align: center; }
  footer { display: flex; justify-content: space-between; gap: 1rem; align-items: baseline; }
  a { color: #a8e9c5; white-space: nowrap; }
</style>
<main>
<header><h1>Lite3 camera</h1><span class="source">__SOURCE_LABEL__</span></header>
<figure>
  <img id="stream" alt="__SOURCE_LABEL__ video stream" hidden>
  <p id="placeholder">Waiting for video</p>
</figure>
<footer>
  <p id="status" role="status">Connecting</p>
  <a id="snapshot" href="/snapshot.jpg" hidden>JPEG snapshot</a>
</footer>
<noscript><p>Enable JavaScript for video or open a <a href="/snapshot.jpg">JPEG snapshot</a>.</p></noscript>
</main>
<script>
const image = document.getElementById('stream');
const statusText = document.getElementById('status');
const placeholder = document.getElementById('placeholder');
const snapshot = document.getElementById('snapshot');
let currentFrameURL;
function showNoVideo(message) {
  image.hidden = true;
  image.removeAttribute('src');
  if (currentFrameURL) URL.revokeObjectURL(currentFrameURL);
  currentFrameURL = null;
  placeholder.hidden = false;
  snapshot.hidden = true;
  statusText.textContent = message;
}
// Read the response ourselves: native <img> MJPEG can silently retain its last
// frame after EOF. Only a successfully decoded, newly received frame is live.
async function streamVideo() {
  const controller = new AbortController();
  let timeout;
  function frameDeadline() {
    clearTimeout(timeout);
    timeout = setTimeout(() => controller.abort(), 2500);
  }
  frameDeadline();
  try {
    const response = await fetch('/stream.mjpg', {cache: 'no-store', signal: controller.signal});
    if (!response.ok) {
      const health = await response.json();
      throw new Error(health.error || 'Waiting for the first frame');
    }
    const reader = response.body.getReader();
    let buffer = new Uint8Array(0);
    const decoder = new TextDecoder();
    while (true) {
      const {value, done} = await reader.read();
      if (done) throw new Error('Video ended. Reconnecting.');
      const combined = new Uint8Array(buffer.length + value.length);
      combined.set(buffer);
      combined.set(value, buffer.length);
      buffer = combined;
      if (buffer.length > 16 * 1024 * 1024) throw new Error('Video frame too large');
      let latest;
      while (true) {
        let end = -1;
        for (let i = 0; i + 3 < buffer.length; i++) {
          if (buffer[i] === 13 && buffer[i+1] === 10 && buffer[i+2] === 13 && buffer[i+3] === 10) {
            end = i + 4;
            break;
          }
        }
        if (end < 0) break;
        const match = /Content-Length:\s*(\d+)/i.exec(decoder.decode(buffer.subarray(0, end)));
        const size = match ? Number(match[1]) : 0;
        if (!size || size > 8 * 1024 * 1024) throw new Error('Invalid video frame');
        if (buffer.length < end + size + 2) break;
        latest = buffer.slice(end, end + size);
        buffer = buffer.slice(end + size + 2);
      }
      if (!latest) continue;
      const nextURL = URL.createObjectURL(new Blob([latest], {type: 'image/jpeg'}));
      const decoded = new Image();
      decoded.src = nextURL;
      try {
        await decoded.decode();
        if (controller.signal.aborted) throw new Error('Video timed out. Reconnecting.');
      } catch (error) {
        URL.revokeObjectURL(nextURL);
        throw error;
      }
      image.src = nextURL;
      if (currentFrameURL) URL.revokeObjectURL(currentFrameURL);
      currentFrameURL = nextURL;
      image.hidden = false;
      placeholder.hidden = true;
      snapshot.hidden = false;
      statusText.textContent = '__SOURCE_LABEL__' === 'Synthetic demo' ? 'Demo streaming' : 'Live';
      frameDeadline();
    }
  } catch (error) {
    showNoVideo(error.name === 'AbortError' ? 'Video timed out. Reconnecting.' : error.message);
  } finally {
    clearTimeout(timeout);
    controller.abort();
    setTimeout(streamVideo, 800);
  }
}
streamVideo();
</script>
</html>
"""


class LatestFrame:
    """One frame shared by the producer and all viewer requests."""

    def __init__(self, source, stale_after=2.0):
        self.source = source
        self.stale_after = stale_after
        self._changed = threading.Condition()
        self._frame = None
        self._captured_at = None
        self._sequence = 0
        self._generation = 0
        self._error = None

    def _age(self):
        if self._captured_at is not None:
            return time.monotonic() - self._captured_at
        return None

    def publish(self, jpeg):
        with self._changed:
            age = self._age()
            if self._error or (age is not None and age >= self.stale_after):
                self._generation += 1
            self._frame = jpeg
            self._captured_at = time.monotonic()
            self._error = None
            self._sequence += 1
            self._changed.notify_all()

    def fail(self, message):
        with self._changed:
            changed = message != self._error
            self._error = message
            self._changed.notify_all()
            return changed

    def snapshot(self):
        with self._changed:
            age = self._age()
            error = self._error
            if error:
                status = "error"
            elif age is None:
                status = "starting"
            elif age >= self.stale_after:
                status = "stale"
                error = "No recent frames. Waiting for video."
            else:
                status = "live"
            health = {
                "source": self.source,
                "status": status,
                "frame_age_s": None if age is None else round(age, 3),
                "error": error,
                "generation": self._generation,
            }
            return health, self._frame if status == "live" else None, self._sequence

    def wait_for_frame(self, sequence, generation):
        with self._changed:
            while not self._error and generation == self._generation:
                age = self._age()
                if age is None or age >= self.stale_after:
                    break
                if sequence != self._sequence:
                    return self._frame, self._sequence
                self._changed.wait(self.stale_after - age)
            return None, sequence


class CameraHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    timeout = 0.2

    def __init__(self, port, frames):
        self.frames = frames
        super().__init__((LOOPBACK, port), CameraRequestHandler)


class CameraRequestHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def log_message(self, *args):
        # Polling health and opening streams should not flood the SSH shell.
        pass

    def do_GET(self):
        try:
            self._get()
        except (ConnectionError, socket.timeout):
            # Closing or refreshing a tab interrupts any write in progress.
            pass

    def _get(self):
        path = self.path.partition("?")[0]
        frames = self.server.frames
        if path == "/":
            source_label = SOURCE_LABELS[frames.source]
            page = PAGE.replace("__SOURCE_LABEL__", source_label).encode()
            self._respond(200, "text/html; charset=utf-8", page)
            return
        if path not in ("/healthz", "/snapshot.jpg", "/stream.mjpg"):
            self._respond(404, "text/plain; charset=utf-8", b"Not found\n")
            return

        health, frame, sequence = frames.snapshot()
        if path == "/healthz" or frame is None:
            status = 200 if frame is not None else 503
            self._respond(status, "application/json", json.dumps(health).encode())
        elif path == "/snapshot.jpg":
            self._respond(200, "image/jpeg", frame)
        else:
            self._stream(frame, sequence, health["generation"])

    def _headers(self, status, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")

    def _respond(self, status, content_type, body):
        self._headers(status, content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, frame, sequence, generation):
        self._headers(200, "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        while frame is not None:
            headers = "Content-Type: image/jpeg\r\nContent-Length: {}\r\n\r\n".format(len(frame))
            self.wfile.write(BOUNDARY + headers.encode("ascii") + frame + b"\r\n")
            frame, sequence = self.server.frames.wait_for_frame(sequence, generation)
        self.wfile.write(b"--frame--\r\n")


def demo_frames(options, stop):
    frames = [base64.b64decode(jpeg) for jpeg in DEMO_JPEGS]
    animation = frames + frames[-2:0:-1]
    frames_per_step = max(1, options.fps // 4)
    for index in itertools.count():
        if stop.is_set():
            break
        yield animation[(index // frames_per_step) % len(animation)]
        stop.wait(1.0 / options.fps)


def _report_capture_error(frames, message):
    if frames.fail(message):
        print(message, file=sys.stderr, flush=True)


def encode_jpeg(cv2, image, quality):
    ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return jpeg.tobytes()


def realsense_frames(options, frames, stop):
    try:
        import pyrealsense2 as rs
        import numpy as np
        import cv2
    except ImportError as error:
        raise RuntimeError(
            "Could not load RealSense Python dependencies (pyrealsense2, numpy, cv2): {}".format(error)
        ) from error

    config = rs.config()
    if options.serial:
        config.enable_device(options.serial)
    config.enable_stream(rs.stream.color, options.width, options.height, rs.format.bgr8, options.fps)
    if options.depth:
        config.enable_stream(rs.stream.depth, options.width, options.height, rs.format.z16, options.fps)
        colorizer = rs.colorizer()

    pipeline = rs.pipeline()
    try:
        pipeline.start(config)
    except RuntimeError as error:
        raise RuntimeError(
            "RealSense could not start. Check device availability, camera ownership and stream profile. "
            "{}".format(error)
        ) from error
    try:
        while not stop.is_set():
            try:
                captured = pipeline.wait_for_frames(1000)
                color = captured.get_color_frame()
                if not color:
                    raise RuntimeError("No RGB frame received")
                image = np.asanyarray(color.get_data())
                if options.depth:
                    depth = captured.get_depth_frame()
                    if not depth:
                        raise RuntimeError("No depth frame received")
                    depth_rgb = np.asanyarray(colorizer.colorize(depth).get_data())
                    depth_bgr = cv2.cvtColor(depth_rgb, cv2.COLOR_RGB2BGR)
                    image = np.hstack((image, depth_bgr))
                jpeg = encode_jpeg(cv2, image, options.quality)
            except Exception as error:
                _report_capture_error(frames, "RealSense frame unavailable: {}".format(error))
                stop.wait(0.25)
                continue
            if not stop.is_set():
                yield jpeg
    finally:
        pipeline.stop()


def v4l2_frames(options, stop):
    try:
        import cv2
    except ImportError as error:
        raise RuntimeError("Could not load OpenCV (cv2) for V4L2: {}".format(error)) from error

    device = options.device
    camera = cv2.VideoCapture(device, cv2.CAP_V4L2)
    try:
        if not camera.isOpened():
            raise RuntimeError("Could not open V4L2 device {}. Check availability and permissions.".format(device))
        settings = (
            ("pixel format", cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV")),
            ("width", cv2.CAP_PROP_FRAME_WIDTH, options.width),
            ("height", cv2.CAP_PROP_FRAME_HEIGHT, options.height),
            ("frame rate", cv2.CAP_PROP_FPS, options.fps),
        )
        for name, prop, value in settings:
            if not camera.set(prop, value):
                raise RuntimeError("Could not set {} on V4L2 device {}".format(name, device))
        while not stop.is_set():
            ok, image = camera.read()
            if not ok or image is None:
                raise RuntimeError("Could not read a frame from V4L2 device {}".format(device))
            if not stop.is_set():
                yield encode_jpeg(cv2, image, options.quality)
    finally:
        camera.release()


def capture(options, frames, stop):
    if options.source == "demo":
        source = demo_frames(options, stop)
    elif options.source == "v4l2":
        source = v4l2_frames(options, stop)
    else:
        source = realsense_frames(options, frames, stop)
    try:
        with contextlib.closing(source):
            for jpeg in source:
                frames.publish(jpeg)
    except Exception as error:
        _report_capture_error(frames, str(error) or type(error).__name__)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=SOURCE_LABELS, default="realsense")
    parser.add_argument("--port", type=int, default=8080, help="loopback port (0 chooses a free port)")
    parser.add_argument("--width", type=int, default=640, help="camera width per stream")
    parser.add_argument("--height", type=int, default=480, help="camera height per stream")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--quality", type=int, default=70, help="camera JPEG quality (1 to 100)")
    parser.add_argument("--depth", action="store_true", help="show colorized RealSense depth beside RGB")
    parser.add_argument("--serial", help="select a RealSense by serial number")
    parser.add_argument("--device", help="V4L2 RGB device path (required for --source v4l2)")
    options = parser.parse_args(argv)
    if options.source == "v4l2":
        if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9_][A-Za-z0-9_.:+-]*)", options.device or ""):
            parser.error("--source v4l2 requires --device /dev/videoN or /dev/v4l/by-id/NAME")
        if options.depth or options.serial is not None:
            parser.error("--depth and --serial cannot be used with --source v4l2")
    elif options.device is not None:
        parser.error("--device requires --source v4l2")
    if not 0 <= options.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if min(options.width, options.height, options.fps) <= 0:
        parser.error("--width, --height and --fps must be positive")
    if not 1 <= options.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    return options


def main(argv=None):
    options = parse_args(argv)
    frames = LatestFrame(options.source)
    stop = threading.Event()
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, request_stop)

    try:
        server = CameraHTTPServer(options.port, frames)
    except OSError as error:
        print("Could not listen on {}:{}: {}".format(LOOPBACK, options.port, error), file=sys.stderr)
        return 1

    producer = threading.Thread(target=capture, args=(options, frames, stop), daemon=True)
    with server:
        producer.start()
        try:
            print("http://{}:{}/".format(LOOPBACK, server.server_port), flush=True)
            while not stopping:
                server.handle_request()
        finally:
            stop.set()
            frames.fail("Viewer stopped")
    producer.join(timeout=3)
    if producer.is_alive():
        print("Camera cleanup timed out", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
