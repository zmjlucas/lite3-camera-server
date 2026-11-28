"""HTTP adapter for shared camera streams and the Lite3 control application."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
import socket
from socketserver import TCPServer, ThreadingMixIn

if __package__:
    from .application import ControlApplication
    from .camera_sources import SOURCE_LABELS
    from .control import ControlError
else:
    from application import ControlApplication
    from camera_sources import SOURCE_LABELS
    from control import ControlError

DEFAULT_BIND = "0.0.0.0"
BOUNDARY = b"--frame\r\n"
WEB_DIR = Path(__file__).resolve().parent / "web"
CONTROL_ERROR_STATUS = {
    "invalid_request": 400,
    "not_found": 404,
    "conflict": 409,
    "locked": 423,
    "unavailable": 503,
}


class CameraHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    timeout = 0.2

    def __init__(self, port, frames, controller=None, bind=DEFAULT_BIND,
                 *, control, arm_gateway=None):
        self.application = ControlApplication(control, arm_gateway)
        self._frames = frames
        self.controller = controller
        super().__init__((bind, port), CameraRequestHandler)

    @property
    def frames(self):
        return self.controller.frames if self.controller is not None else self._frames

    def server_bind(self):
        # HTTPServer resolves its address to a hostname. DNS can delay this call, even for loopback.
        # This server needs only its numeric local address.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class CameraRequestHandler(BaseHTTPRequestHandler):
    def version_string(self):
        return "Lite3Dev"

    def setup(self):
        self.request.settimeout(2)
        super().setup()

    def log_message(self, *args):
        # Omit request logs to keep repeated health checks out of the SSH terminal.
        pass

    def do_GET(self):
        try:
            self._get()
        except (ConnectionError, socket.timeout):
            # A tab close or reload can interrupt a write.
            pass

    def _get(self):
        if not self._same_origin():
            return
        path = self.path.partition("?")[0]
        if path == "/api/status":
            self._json(200, self.server.application.status())
            return
        if path == "/control":
            page = (WEB_DIR / "control.html").read_bytes()
            self._respond(200, "text/html; charset=utf-8", page)
            return
        if path == "/static/dev_client.js":
            self._respond(200, "application/javascript; charset=utf-8", (WEB_DIR / "dev_client.js").read_bytes())
            return
        frames = self.server.frames
        if path == "/":
            source_label = SOURCE_LABELS[frames.source]
            template = (WEB_DIR / "camera.html").read_text(encoding="utf-8")
            page = template.replace("__SOURCE_LABEL__", source_label).encode()
            self._respond(200, "text/html; charset=utf-8", page)
            return
        if path not in ("/healthz", "/snapshot.jpg", "/stream.mjpg", "/sources"):
            self._respond(404, "text/plain; charset=utf-8", b"Not found\n")
            return

        if path == "/sources":
            try:
                controller = getattr(self.server, "controller", None)
                catalog = controller.catalog() if controller else {"sources": [], "selected": None}
                self._json(200, catalog)
            except (OSError, RuntimeError) as error:
                self._json(503, {"error": "Cannot discover camera sources: " + str(error)})
            return

        health, frame, sequence = frames.snapshot()
        if path == "/healthz" or frame is None:
            status = 200 if frame is not None else 503
            self._respond(status, "application/json", json.dumps(health).encode())
        elif path == "/snapshot.jpg":
            self._respond(200, "image/jpeg", frame)
        else:
            self._stream(frames, frame, sequence, health["generation"])

    def _same_origin(self):
        origins = self.headers.get_all("Origin", [])
        if origins and (len(origins) != 1 or origins[0].lower() != "http://" + self.headers.get("Host", "").lower()):
            self._respond(403, "text/plain; charset=utf-8", b"Same origin required\n")
            return False
        return True

    def do_POST(self):
        try:
            self._post()
        except (ConnectionError, socket.timeout):
            pass

    def _post(self):
        if not self._same_origin():
            return
        if self.path.partition("?")[0].startswith("/api/"):
            self._control_post()
            return
        if self.path.partition("?")[0] != "/source":
            self._json(404, {"error": "Not found"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]) or self.headers.get_all("Transfer-Encoding"):
            self._json(400, {"error": "One valid Content-Length is required"})
            return
        size = int(lengths[0])
        if size > 4096:
            self._json(413, {"error": "Selection request is too large"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(400, {"error": "Use an application/json selection request"})
            return
        try:
            body = self.rfile.read(size)
            if len(body) != size:
                raise ValueError("Incomplete selection request")
            selection = json.loads(body.decode("utf-8"))
            if (not isinstance(selection, dict) or set(selection) != {"id"}
                    or not isinstance(selection["id"], str) or not 1 <= len(selection["id"]) <= 128):
                raise ValueError("Select a camera id from the current source list")
            controller = getattr(self.server, "controller", None)
            if controller is None:
                raise RuntimeError("Camera selection is unavailable in this server")
            catalog = controller.select(selection["id"])
        except (ValueError, UnicodeError, RecursionError) as error:
            self._json(400, {"error": str(error)})
        except (OSError, RuntimeError) as error:
            self._json(409, {"error": str(error)})
        else:
            self._json(200, catalog)

    def _control_post(self):
        action = self.path.partition("?")[0][5:]
        allowed = {"control/acquire", "control/heartbeat", "control/release", "control/estop",
                   "control/reset", "dog/stand", "dog/velocity", "dog/stop", "arm/jog",
                   "arm/state", "arm/reconnect", "arm/operation", "arm/stop"}
        if action not in allowed:
            self._json(404, {"error": "Unknown control operation"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0])
                or self.headers.get_all("Transfer-Encoding")):
            self._json(400, {"error": "One valid Content-Length is required"})
            return
        size = int(lengths[0])
        if size > 4096:
            self._json(413, {"error": "Control request is too large"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(400, {"error": "Use application/json"})
            return
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON field")
                result[key] = value
            return result
        def reject_constant(value):
            raise ValueError("Non-finite JSON number")
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("Incomplete request body")
            body = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=reject_constant)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            result = self.server.application.dispatch(action, body)
            self._json(200, result)
        except ControlError as error:
            self._json(CONTROL_ERROR_STATUS[error.code], {"error": str(error), "code": error.code})
        except (ValueError, UnicodeError, RecursionError) as error:
            self._json(400, {"error": str(error)})
        except (OSError, RuntimeError) as error:
            self._json(503, {"error": str(error)})

    def _json(self, status, value):
        self._respond(status, "application/json", json.dumps(value).encode())

    def _headers(self, status, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Lite3-Camera-Protocol", "1")
        self.send_header("X-Lite3-Dev-Protocol", "1")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; img-src blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")

    def _respond(self, status, content_type, body):
        self._headers(status, content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, frames, frame, sequence, generation):
        self._headers(200, "multipart/x-mixed-replace; boundary=frame")
        self.send_header("X-Camera-Source", frames.source)
        self.send_header("X-Camera-Source-Id", frames.source_id or "")
        self.end_headers()
        while frame is not None:
            headers = "Content-Type: image/jpeg\r\nContent-Length: {}\r\n\r\n".format(len(frame))
            self.wfile.write(BOUNDARY + headers.encode("ascii") + frame + b"\r\n")
            frame, sequence = frames.wait_for_frame(sequence, generation)
        self.wfile.write(b"--frame--\r\n")
