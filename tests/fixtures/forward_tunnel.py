"""Local TCP forwarding substitute for SSH in existing-view integration tests."""
import select
import socket
import socketserver
import sys
import time


class Forwarder(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            with socket.create_connection(("127.0.0.1", self.server.remote_port), timeout=1) as remote:
                peers = {self.request: remote, remote: self.request}
                while True:
                    readable, _, _ = select.select(list(peers), [], [], 2)
                    if not readable:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        peers[source].sendall(data)
        except OSError:
            pass


if __name__ == "__main__":
    local_port, remote_port = map(int, sys.argv[1:3])
    time.sleep(float(sys.argv[3]))
    with socketserver.ThreadingTCPServer(("127.0.0.1", local_port), Forwarder) as server:
        server.daemon_threads = True
        server.remote_port = remote_port
        server.serve_forever()
