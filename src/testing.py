"""Start a real HTTP simulation for integration tests (Python 3.8+)."""
from contextlib import contextmanager
from http.client import HTTPException
import math
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Iterator
from urllib.request import ProxyHandler, build_opener


@contextmanager
def simulated_server(*, startup_timeout: float = 10.0,
                     shutdown_timeout: float = 5.0) -> Iterator[str]:
    """Yield a ready loopback URL with an OS-assigned port, then reap the server.

    Startup errors include the child's output. After shutdown_timeout seconds,
    a child that still runs is killed and waited for. Exceptions in the caller's
    test propagate after cleanup. Simulation uses no camera, serial port or SSH.
    """
    for name, value in (('startup_timeout', startup_timeout), ('shutdown_timeout', shutdown_timeout)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(name + ' must be finite and positive')

    with tempfile.TemporaryDirectory(prefix='lite3-simulation-') as directory:
        log = Path(directory) / 'server.log'
        # A file captures both streams without pipe backpressure or reader threads.
        # Read it through a separate handle so the child keeps its write offset.
        with log.open('wb') as output:
            process = subprocess.Popen(
                [sys.executable, '-E', '-S', '-B', '-u', str(Path(__file__).with_name('simulation.py')),
                 '--port', '0'], stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + startup_timeout
                opener = build_opener(ProxyHandler({}))
                url = None
                ready = False
                detail = 'The server did not report a listening URL.'
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('Simulation exited with code {} before HTTP readiness.\n{}'.format(
                            process.returncode, log.read_text(encoding='utf-8', errors='replace')))
                    if url is None:
                        for line in log.read_text(encoding='utf-8', errors='replace').splitlines():
                            match = re.fullmatch(r'http://127\.0\.0\.1:([0-9]+)/?', line.strip())
                            if match and 1 <= int(match.group(1)) <= 65535:
                                url = line.strip().rstrip('/')
                                break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if url is not None:
                        # /healthz reports camera readiness and returns 503 in camera-free simulation.
                        endpoint = url + '/api/status'
                        try:
                            with opener.open(endpoint, timeout=min(0.2, remaining)) as response:
                                identified = (response.status == 200 and response.geturl() == endpoint and
                                              response.headers.get('X-Lite3-Dev-Protocol') == '1' and
                                              response.headers.get('X-Lite3-Environment') == 'dev' and
                                              response.headers.get('X-Lite3-Simulated') == 'true')
                                response.read()
                            ready = identified and process.poll() is None
                            if ready:
                                break
                            detail = 'The HTTP response did not identify a ready simulation.'
                        except (OSError, HTTPException) as error:
                            detail = str(error)
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
                if not ready or url is None:
                    raise RuntimeError('Simulation was not ready within {} seconds: {}\n{}'.format(
                        startup_timeout, detail, log.read_text(encoding='utf-8', errors='replace')))
                yield url
            finally:
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=shutdown_timeout)
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    # Reap the child even if Ctrl+C interrupts the graceful wait.
                    if process.poll() is None:
                        process.kill()
                    process.wait()
