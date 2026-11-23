"""Check that a remote loopback port is free before starting a camera session."""
import socket
import sys


with socket.socket() as listener:
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", int(sys.argv[1])))
        listener.listen(1)
    except OSError as exc:
        sys.exit("Cannot use the remote camera port. Select another port with --remote-port: " + str(exc))
