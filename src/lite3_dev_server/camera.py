"""Synchronous camera HTTP v1 client; Python 3.8+, standard library only."""
from email.message import Message
from http.client import HTTPException, HTTPMessage, HTTPResponse
import json
import math
import re
from types import TracebackType
from typing import Callable, Iterator, Literal, Mapping, NoReturn, Optional, Type, TypeVar, Union, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from . import _responses
from .api import CAMERA_PROTOCOL_HEADER, CAMERA_PROTOCOL_VERSION, CameraHealth, CameraSourceCatalog
from .client import _NoRedirect, _parse_json_float, _unique_json_object


_Reply = TypeVar('_Reply')
_HTTPResponse = Union[HTTPResponse, HTTPError]
_FAILURES = (URLError, OSError, HTTPException, ValueError, RecursionError)
_MAX_JSON = 65536
_MAX_HEADERS = 8192


def _response_status(response: _HTTPResponse) -> int:
    # HTTPError.status delegates to its body on Python 3.8.
    return response.code if isinstance(response, HTTPError) else response.status


class CameraClientError(RuntimeError):
    """Camera HTTP, transport or response validation failure."""

    status: Optional[int]

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class CameraFrameUnavailable(CameraClientError):
    """The server has no usable frame; inspect health for the state and reason."""

    health: CameraHealth

    def __init__(self, health: CameraHealth) -> None:
        super().__init__(health['error'] or 'Camera frame unavailable: ' + health['status'], 503)
        self.health = health


def _single_header(headers: Message, name: str) -> str:
    values = headers.get_all(name, [])
    if len(values) != 1:
        raise ValueError('Expected one ' + name + ' header')
    return str(values[0]).strip(' \t')


def _content_type(headers: Message, expected: str) -> None:
    _single_header(headers, 'Content-Type')
    if headers.get_content_type() != expected:
        raise ValueError('Expected ' + expected + ' response')


def _read_bounded(response: _HTTPResponse, maximum: int) -> bytes:
    expected = None
    if response.headers.get_all('Content-Length') is not None:
        length = _single_header(response.headers, 'Content-Length')
        if not re.fullmatch(r'[0-9]+', length) or int(length) > maximum:
            raise ValueError('Invalid or oversized camera Content-Length')
        expected = int(length)
    data = bytearray()
    while len(data) <= maximum:
        chunk = response.read(min(65536, maximum + 1 - len(data)))
        if not chunk:
            if expected is not None and len(data) != expected:
                raise ValueError('Truncated camera response')
            return bytes(data)
        data.extend(chunk)
    raise ValueError('Camera response exceeds the size limit')


def _read_json(response: _HTTPResponse) -> object:
    _content_type(response.headers, 'application/json')
    raw = _read_bounded(response, _MAX_JSON)
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_json_object,
                      parse_constant=_parse_json_float, parse_float=_parse_json_float)


def _raise_rejection(value: object, status: int, frame: bool = False) -> NoReturn:
    if frame and status == 503 and isinstance(value, dict) and 'status' in value:
        health = _responses.camera_health(value)
        if health['status'] == 'live':
            raise ValueError('Unavailable camera response claims a live frame')
        raise CameraFrameUnavailable(health)
    raise CameraClientError(_responses.error_response(value)['error'], status)


class CameraStream(Iterator[bytes]):
    """An open MJPEG response. Use with, or call close() after an early break.

    A normal multipart terminator ends iteration and closes the response. Capture
    failure, staleness and source switching can all end a stream; inspect health()
    and explicitly open another stream when appropriate. No automatic reconnect.
    Each iterator is intended for consumption by one thread.
    """

    def __init__(self, response: _HTTPResponse, boundary: bytes, max_frame_bytes: int) -> None:
        self._response = response
        self._boundary = b'--' + boundary
        self._max_frame_bytes = max_frame_bytes
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._response.close()

    def __enter__(self) -> 'CameraStream':
        return self

    def __exit__(self, exception_type: Optional[Type[BaseException]],
                 exception: Optional[BaseException], traceback: Optional[TracebackType]) -> Literal[False]:
        self.close()
        return False

    def __iter__(self) -> 'CameraStream':
        return self

    def _line(self, maximum: int) -> bytes:
        line = self._response.readline(maximum + 1)
        if len(line) > maximum or not line.endswith(b'\r\n'):
            raise ValueError('Truncated or oversized MJPEG header')
        return line

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self._response.read(size - len(data))
            if not chunk:
                raise ValueError('Truncated MJPEG frame')
            data.extend(chunk)
        return bytes(data)

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            line = self._line(_MAX_HEADERS)
            if line == self._boundary + b'--\r\n':
                self.close()
                raise StopIteration
            if line != self._boundary + b'\r\n':
                raise ValueError('Invalid MJPEG boundary')
            headers = HTTPMessage()
            remaining = _MAX_HEADERS
            while True:
                line = self._line(remaining)
                remaining -= len(line)
                if line == b'\r\n':
                    break
                name, separator, value = line[:-2].partition(b':')
                if not separator or re.fullmatch(rb'[!#$%&\'*+.^_`|~0-9A-Za-z-]+', name) is None:
                    raise ValueError('Invalid MJPEG part header')
                headers.add_header(name.decode('ascii'), value.decode('ascii').strip(' \t'))
            _content_type(headers, 'image/jpeg')
            length = _single_header(headers, 'Content-Length')
            if not re.fullmatch(r'[0-9]+', length) or not 0 < int(length) <= self._max_frame_bytes:
                raise ValueError('Invalid or oversized MJPEG frame length')
            frame = self._read_exact(int(length))
            if self._read_exact(2) != b'\r\n':
                raise ValueError('Invalid MJPEG frame terminator')
            return frame
        except _FAILURES as error:
            self.close()
            raise CameraClientError('Camera stream failed: ' + str(error),
                                    _response_status(self._response)) from None


class CameraClient:
    """Explicit camera calls, independent of robot control ownership.

    timeout bounds blocking socket operations, including stream reads. It is
    finite, positive and at most 10 seconds. max_frame_bytes bounds each JPEG
    (default 16 MiB). JPEG bytes are delivered directly for application decoding.
    """

    def __init__(self, url: str, timeout: float = 5.0, max_frame_bytes: int = 16 * 1024 * 1024) -> None:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                '?' in url or '#' in url or parsed.path not in ('', '/')):
            raise ValueError('url must be an HTTP(S) server origin without credentials, query or path')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or not 0 < timeout <= 10):
            raise ValueError('timeout must be finite, positive and at most 10 seconds')
        if isinstance(max_frame_bytes, bool) or not isinstance(max_frame_bytes, int) or max_frame_bytes <= 0:
            raise ValueError('max_frame_bytes must be a positive integer')
        self.url = url.rstrip('/')
        self.timeout = float(timeout)
        self.max_frame_bytes = max_frame_bytes
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _open(self, path: str, accept: str,
              body: Optional[Mapping[str, object]] = None) -> _HTTPResponse:
        response: Optional[_HTTPResponse] = None
        try:
            headers = {'Accept': accept}
            data = None
            if body is not None:
                headers['Content-Type'] = 'application/json'
                data = json.dumps(body, allow_nan=False).encode('utf-8')
            request = Request(self.url + path, data=data, headers=headers)
            try:
                response = cast(HTTPResponse, self._opener.open(request, timeout=self.timeout))
            except HTTPError as rejected:
                response = rejected
            if _single_header(response.headers, CAMERA_PROTOCOL_HEADER) != CAMERA_PROTOCOL_VERSION:
                raise ValueError('Unsupported Lite3 camera protocol version')
            return response
        except _FAILURES as error:
            status = _response_status(response) if response is not None else None
            if response is not None:
                response.close()
            raise CameraClientError('Camera request failed: ' + str(error), status) from None

    def _json(self, path: str, validate: Callable[[object], _Reply],
              body: Optional[Mapping[str, object]] = None) -> _Reply:
        with self._open(path, 'application/json', body) as response:
            status = _response_status(response)
            try:
                value = _read_json(response)
                if path == '/healthz' and status == 503:
                    health = _responses.camera_health(value)
                    if health['status'] == 'live':
                        raise ValueError('Unavailable camera response claims a live frame')
                elif status != 200:
                    _raise_rejection(value, status)
                return validate(value)
            except _FAILURES as error:
                raise CameraClientError('Invalid camera response: ' + str(error), status) from None

    def sources(self) -> CameraSourceCatalog:
        """Read the catalog, including unavailable devices and their reasons."""
        return self._json('/sources', _responses.camera_sources)

    def health(self) -> CameraHealth:
        """Return live, starting, stale or error health, including HTTP 503 health."""
        return self._json('/healthz', _responses.camera_health)

    def select_source(self, source_id: str) -> CameraSourceCatalog:
        """Switch the shared capture for ALL viewers using an ID from sources().

        Capture starts asynchronously; health() reports readiness after selection.
        Device availability is checked by the server on every explicit request.
        """
        if not isinstance(source_id, str) or not 1 <= len(source_id) <= 128:
            raise ValueError('Select a camera id from the current source list (1 to 128 characters)')
        return self._json('/source', _responses.camera_sources, {'id': source_id})

    def _image_response(self, path: str, content_type: str) -> _HTTPResponse:
        response = self._open(path, content_type)
        try:
            if _response_status(response) != 200:
                _raise_rejection(_read_json(response), _response_status(response), frame=True)
            _content_type(response.headers, content_type)
            return response
        except BaseException as error:
            response.close()
            if isinstance(error, _FAILURES):
                raise CameraClientError('Invalid camera response: ' + str(error),
                                        _response_status(response)) from None
            raise

    def snapshot(self) -> bytes:
        """Read one JPEG; raise CameraFrameUnavailable with health when absent."""
        with self._image_response('/snapshot.jpg', 'image/jpeg') as response:
            try:
                frame = _read_bounded(response, self.max_frame_bytes)
                if not frame:
                    raise ValueError('Empty JPEG snapshot')
                return frame
            except _FAILURES as error:
                raise CameraClientError('Camera snapshot failed: ' + str(error),
                                        _response_status(response)) from None

    def stream(self) -> CameraStream:
        """Open an MJPEG iterator. Use ``with camera.stream() as frames: ...``."""
        response = self._image_response('/stream.mjpg', 'multipart/x-mixed-replace')
        try:
            parameters = response.headers.get_params() or []
            boundaries = [value for name, value in parameters if name.lower() == 'boundary']
            if len(boundaries) != 1 or not isinstance(boundaries[0], str):
                raise ValueError('Expected one MJPEG boundary parameter')
            boundary = boundaries[0].encode('ascii')
            if (not re.fullmatch(rb"[0-9A-Za-z'()+_,./:=? -]{1,70}", boundary) or
                    boundary.endswith(b' ')):
                raise ValueError('Invalid MJPEG boundary parameter')
            return CameraStream(response, boundary, self.max_frame_bytes)
        except BaseException as error:
            response.close()
            if isinstance(error, _FAILURES):
                raise CameraClientError('Invalid camera stream: ' + str(error),
                                        _response_status(response)) from None
            raise
