"""Loopback regressions for absolute HTTP request deadlines."""
import json
from pathlib import Path
import socketserver
import ssl
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from src.lite3_dev_server import DevClient, DevClientError
from src.lite3_dev_server._transport import DeadlineHTTPSHandler
from test_dev_client import control_status


# Public synthetic credentials used only by temporary loopback TLS servers.
_CERTIFICATE = '''-----BEGIN CERTIFICATE-----
MIIBjjCCATSgAwIBAgIUF3ptBwttCFJwhmQ1DIv90HL35SAwCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJbG9jYWxob3N0MB4XDTI2MTEyODEzNDQ0MloXDTM2MTEyNTEz
NDQ0MlowFDESMBAGA1UEAwwJbG9jYWxob3N0MFkwEwYHKoZIzj0CAQYIKoZIzj0D
AQcDQgAEJLhbOxhRmrQKAUURHIpR3gQCczDkJGpeWn9jx0UTL9T9ivLdksx8/m3X
hFWU7JQLJysW2/txMP8/bzyf5KwhfqNkMGIwHQYDVR0OBBYEFAX1akGZLxVjExpc
kVbQKI9VZCy0MB8GA1UdIwQYMBaAFAX1akGZLxVjExpckVbQKI9VZCy0MA8GA1Ud
EwEB/wQFMAMBAf8wDwYDVR0RBAgwBocEfwAAATAKBggqhkjOPQQDAgNIADBFAiEA
9OR4POvg+/l1J5ed9noTkhbaPliQFtP6req15COiPj0CIGveDeykAbCVOVQOiiDB
m/HoGVGkFJTxDjppoX9lmxoH
-----END CERTIFICATE-----
'''
_PRIVATE_KEY = '''-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgOa0lazqdUojrj+x9
DC50SIWSv+iEIAqUYTgIPxI8S7qhRANCAAQkuFs7GFGatAoBRREcilHeBAJzMOQk
al5af2PHRRMv1P2K8t2SzHz+bdeEVZTslAsnKxbb+3Ew/z9vPJ/krCF+
-----END PRIVATE KEY-----
'''


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class DeadlineTests(unittest.TestCase):
    timeout = .08

    def server(self, kind, tls=False):
        body = json.dumps(control_status()).encode()
        headers = ('HTTP/1.1 200 OK\r\nX-Lite3-Dev-Protocol: 1\r\n'
                   'Content-Length: {}\r\nConnection: close\r\n\r\n').format(len(body)).encode()
        workers = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(inner):
                workers.append(threading.current_thread())
                inner.request.settimeout(1)
                request = b''
                while b'\r\n\r\n' not in request:
                    chunk = inner.request.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                try:
                    if kind == 'complete':
                        inner.request.sendall(headers + body)
                    elif kind == 'combined':
                        time.sleep(.06)
                        inner.request.sendall(headers)
                        time.sleep(.06)
                        inner.request.sendall(body)
                    else:
                        delayed = headers if kind == 'headers' else body
                        if kind == 'body':
                            inner.request.sendall(headers)
                        width = max(1, (len(delayed) + 11) // 12)
                        for start in range(0, len(delayed), width):
                            inner.request.sendall(delayed[start:start + width])
                            time.sleep(.025)
                        if kind == 'headers':
                            inner.request.sendall(body)
                except (OSError, ConnectionError):
                    pass

        server = _Server(('127.0.0.1', 0), Handler)
        if tls:
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            certificate = Path(directory.name) / 'certificate.pem'
            key = Path(directory.name) / 'key.pem'
            certificate.write_text(_CERTIFICATE)
            key.write_text(_PRIVATE_KEY)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(certificate), str(key))
            server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': .01}, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(1)
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())

        self.addCleanup(cleanup)
        return '{}://127.0.0.1:{}'.format('https' if tls else 'http', server.server_address[1])

    def client(self, url):
        # Certificate validation is irrelevant to these synthetic TLS streams.
        context = ssl._create_unverified_context()
        with patch('src.lite3_dev_server.client.DeadlineHTTPSHandler',
                   lambda: DeadlineHTTPSHandler(context=context)):
            return DevClient(url, timeout=self.timeout)

    def assert_status_deadline(self, kind, tls=False):
        client = self.client(self.server(kind, tls=tls))
        started = time.monotonic()
        with self.assertRaisesRegex(DevClientError, 'timed out|deadline exceeded'):
            client.status()
        self.assertLess(time.monotonic() - started, .2)

    def test_header_and_body_time_count_together(self):
        self.assert_status_deadline('combined')

    def test_https_trickling_headers_and_body_share_deadline(self):
        for kind in ('headers', 'body'):
            with self.subTest(kind=kind):
                self.assert_status_deadline(kind, tls=True)

    def test_https_complete_response_remains_supported(self):
        self.assertEqual(self.client(self.server('complete', tls=True)).status(), control_status())


if __name__ == '__main__':
    unittest.main()
