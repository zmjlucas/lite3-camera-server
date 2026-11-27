"""Small synchronous Lite3 Dev Server SDK (Python 3.8+, standard library only).

No operation retries or acquires ownership automatically. Call heartbeat explicitly
while holding a lease. Velocity TTL is independent of that lease heartbeat.
"""
from http.client import HTTPException
import json
import math
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class DevClientError(RuntimeError):
    """The server rejected a request, or its response could not be trusted."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # A redirect must never forward the control token to a different service.
        return None


class DevClient:
    """Explicit, serialized API calls; context exit releases an acquired lease.

    ``stop()``, ``estop()``, ``reset()`` and any failed request forget ownership.
    Reacquire and explicitly prepare the dog again before further motion. A
    successful response is software state, never proof of physical robot state.
    """

    MAX_RESPONSE = 65536

    def __init__(self, url, token, timeout=2.0):
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username is not None or parsed.password is not None or
                parsed.query or parsed.fragment or parsed.path not in ('', '/')):
            raise ValueError('url must be an HTTP(S) server origin without credentials, query or path')
        if not isinstance(token, str) or not token.strip() or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise ValueError('token must contain printable non-space ASCII characters')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or not 0 < timeout <= 10):
            raise ValueError('timeout must be finite, positive and at most 10 seconds')
        self.url = url.rstrip('/')
        self.timeout = float(timeout)
        self._token = token
        self._lease = None
        self._sequence = 0
        self._lock = threading.RLock()
        # Avoid environment proxies and HTTP redirects carrying local credentials.
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    @property
    def lease(self):
        """Current private lease capability, or None; do not log this value."""
        with self._lock:
            return self._lease

    def _request(self, action, body=None):
        with self._lock:
            try:
                headers = {'Accept': 'application/json'}
                data = None
                if body is not None:
                    data = json.dumps(body, allow_nan=False).encode('utf-8')
                    headers.update({'Content-Type': 'application/json',
                                    'Authorization': 'Bearer ' + self._token})
                request = Request(self.url + '/api/' + action, data=data, headers=headers)
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read(self.MAX_RESPONSE + 1)
                if len(raw) > self.MAX_RESPONSE:
                    raise DevClientError('Server response is too large')
                result = json.loads(raw.decode('utf-8'))
                if not isinstance(result, dict):
                    raise DevClientError('Server response is not a JSON object')
                return result
            except HTTPError as error:
                self._lease = None
                message = 'Control request rejected (HTTP {})'.format(error.code)
                try:
                    detail = json.loads(error.read(self.MAX_RESPONSE).decode('utf-8'))
                    if isinstance(detail, dict) and isinstance(detail.get('error'), str):
                        message = detail['error']
                except (ValueError, UnicodeError, OSError):
                    pass
                finally:
                    error.close()
                raise DevClientError(message, error.code) from None
            except (URLError, OSError, HTTPException, ValueError, UnicodeError, DevClientError) as error:
                self._lease = None
                if isinstance(error, DevClientError):
                    raise
                raise DevClientError('Control request failed; ownership forgotten: ' + str(error)) from None

    def _owned(self, action, values=None, motion=False):
        with self._lock:
            if self._lease is None:
                raise DevClientError('Acquire control explicitly before this operation')
            body = dict(values or {})
            body['lease'] = self._lease
            if motion:
                self._sequence += 1
                body['seq'] = self._sequence
            return self._request(action, body)

    def status(self):
        return self._request('status')

    def acquire(self, owner='python-sdk'):
        with self._lock:
            if self._lease is not None:
                raise DevClientError('Release the current lease before acquiring again')
            result = self._request('control/acquire', {'owner': owner})
            if not isinstance(result.get('lease'), str) or not result['lease']:
                raise DevClientError('Server did not provide a control lease')
            self._lease = result['lease']
            self._sequence = 0
            return result

    def heartbeat(self):
        return self._owned('control/heartbeat')

    def stand(self):
        """Start staged stand preparation; wait for status dog.phase == ready."""
        return self._owned('dog/stand', motion=True)

    def velocity(self, vx=0.0, vy=0.0, yaw=0.0, ttl=0.3):
        """Submit m/s, m/s, rad/s for at most ttl seconds; no automatic renewal."""
        return self._owned('dog/velocity', {'vx': vx, 'vy': vy, 'yaw': yaw, 'ttl': ttl}, motion=True)

    def stop(self):
        """Stop the dog, revoke ownership, and cancel preparation (observer allowed)."""
        with self._lock:
            body = {'lease': self._lease} if self._lease else {}
            self._lease = None
            return self._request('dog/stop', body)

    def release(self):
        with self._lock:
            lease, self._lease = self._lease, None
            if lease is None:
                return None
            return self._request('control/release', {'lease': lease})

    def estop(self):
        with self._lock:
            self._lease = None
            return self._request('control/estop', {})

    def reset(self):
        with self._lock:
            self._lease = None
            return self._request('control/reset', {})

    def arm_jog(self, joint, delta, speed=2.0):
        """Simulation only: relative joint delta in degrees, speed in degrees/s."""
        return self._owned('arm/jog', {'joint': joint, 'delta': delta, 'speed': speed}, motion=True)

    def arm_state(self):
        return self._request('arm/state', {})

    def arm_reconnect(self):
        """Explicitly reopen the read-only arm connection after a failure."""
        return self._request('arm/reconnect', {})

    def close(self):
        self.release()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        try:
            self.close()
        except DevClientError:
            if exception_type is None:
                raise
        return False
