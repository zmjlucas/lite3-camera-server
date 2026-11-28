"""Synchronous HTTP transport with one deadline across socket operations.

The operating system's hostname resolver remains outside socket timeout control.
Resolved connection attempts, TLS, sends, headers and response bodies share the
remaining request budget. Reading a trickling reply never renews that budget.
"""
from functools import partial
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection
import io
import math
import socket
import ssl
import time
from typing import TYPE_CHECKING, Any, Callable, Optional, Tuple
from urllib.request import HTTPHandler, HTTPSHandler, Request

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer


class _Budget:
    def __init__(self, timeout: Optional[float]) -> None:
        if timeout is None or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('HTTP requests require a finite positive timeout')
        self.deadline = time.monotonic() + timeout

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('HTTP request deadline exceeded')
        return remaining


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock: socket.socket, raw: io.RawIOBase, budget: _Budget) -> None:
        super().__init__()
        self._socket = sock
        self._raw = raw
        self._budget = budget

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: 'WriteableBuffer') -> Optional[int]:
        self._socket.settimeout(self._budget.remaining())
        return self._raw.readinto(buffer)

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


class _DeadlineHTTPConnection(HTTPConnection):
    def __init__(self, host: str, port: Optional[int] = None, timeout: Optional[float] = None,
                 source_address: Optional[Tuple[str, int]] = None, blocksize: int = 8192) -> None:
        budget = self._budget = _Budget(timeout)
        super().__init__(host, port, timeout, source_address, blocksize)
        self._create_connection = self._connect

        class Response(HTTPResponse):
            def __init__(self, sock: socket.socket, debuglevel: int = 0,
                         method: Optional[str] = None, url: Optional[str] = None) -> None:
                super().__init__(sock, debuglevel, method, url)
                # HTTPResponse has performed no reads yet. Keep SocketIO's
                # ownership so urllib closing its connection preserves this fp.
                self.fp = io.BufferedReader(_DeadlineReader(sock, self.fp.detach(), budget))

        self.response_class = Response

    def _connect(self, address: Tuple[str, int], timeout: Optional[float],
                 source_address: Optional[Tuple[str, int]]) -> socket.socket:
        # create_connection applies its timeout separately to each address.
        # Check this request's remaining budget between all resolved candidates.
        host, port = address
        addresses = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        self._budget.remaining()
        error: Optional[OSError] = None
        for family, kind, protocol, _, destination in addresses:
            sock = None
            try:
                sock = socket.socket(family, kind, protocol)
                sock.settimeout(self._budget.remaining())
                if source_address is not None:
                    sock.bind(source_address)
                sock.connect(destination)
                sock.settimeout(self._budget.remaining())
                return sock
            except OSError as caught:
                error = caught
                if sock is not None:
                    sock.close()
                self._budget.remaining()
            except BaseException:
                # The connection owns this candidate only after _connect returns.
                # Preserve interrupts while closing the otherwise unowned socket.
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
                raise
        if error is not None:
            raise error
        raise OSError('Hostname resolution returned no addresses')

    def connect(self) -> None:
        self.timeout = self._budget.remaining()
        super().connect()
        if self.sock is not None:
            self.sock.settimeout(self._budget.remaining())

    def send(self, data: Any) -> None:
        if self.sock is None and self.auto_open:
            self.connect()
        if self.sock is not None:
            self.sock.settimeout(self._budget.remaining())
        super().send(data)


class _DeadlineHTTPSConnection(HTTPSConnection, _DeadlineHTTPConnection):
    """HTTPS delegates TCP setup through the deadline connection before TLS."""


def _open_connection(connection_type: Callable[..., _DeadlineHTTPConnection], deadline: Optional[float],
                     host: str, **kwargs: Any) -> _DeadlineHTTPConnection:
    connection = connection_type(host, **kwargs)
    if deadline is not None:
        connection._budget.deadline = min(connection._budget.deadline, deadline)
    return connection


class DeadlineHTTPHandler(HTTPHandler):
    def http_open(self, req: Request) -> HTTPResponse:
        connection = partial(_open_connection, _DeadlineHTTPConnection, getattr(req, '_lite3_deadline', None))
        return self.do_open(connection, req)


class DeadlineHTTPSHandler(HTTPSHandler):
    _context: Optional[ssl.SSLContext]

    def https_open(self, req: Request) -> HTTPResponse:
        connection = partial(_open_connection, _DeadlineHTTPSConnection, getattr(req, '_lite3_deadline', None))
        return self.do_open(connection, req, context=self._context)
