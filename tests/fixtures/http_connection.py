"""An in-memory socket for tests of the real HTTP request handler."""
import io


class MemoryConnection:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def settimeout(self, timeout):
        pass

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.extend(data)
