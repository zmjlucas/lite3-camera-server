"""Camera SDK contracts over real loopback HTTP and controlled byte streams."""
import argparse
import copy
from http.client import HTTPMessage, IncompleteRead
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import lite3_dev_server as sdk
from src.camera import CaptureController, LatestFrame
from src.control import ControlService
from src.http_server import CameraHTTPServer


JPEG = (Path(__file__).parent / 'fixtures/camera-frame.jpg').read_bytes()
HEALTH = {'source': 'v4l2', 'source_id': None, 'status': 'starting',
          'frame_age_s': None, 'error': None, 'generation': 0}
CATALOG = {'sources': [{'id': 'camera-one', 'label': 'Camera one', 'device': '/dev/video4',
                        'backend': 'v4l2', 'available': True, 'error': None}], 'selected': None}


def headers(content_type, versions=('1',)):
    result = HTTPMessage()
    result['Content-Type'] = content_type
    for version in versions:
        result['X-Lite3-Camera-Protocol'] = version
    return result


class Response(io.BytesIO):
    def __init__(self, body, content_type='application/json', versions=('1',), fragment=None):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = headers(content_type, versions)
        self.status = 200
        self.fragment = fragment

    def read(self, size=-1):
        if self.fragment is not None and size > self.fragment:
            size = self.fragment
        return super().read(size)


class LegacyHTTPError(HTTPError):
    @property
    def status(self):
        # Python 3.8 delegates this attribute to fp; getcode()/code belong to the error.
        return self.fp.status


def multipart(*frames, boundary=b'frame'):
    return b''.join(b'--' + boundary + b'\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                    str(len(frame)).encode() + b'\r\n\r\n' + frame + b'\r\n' for frame in frames) + \
        b'--' + boundary + b'--\r\n'


class CameraHTTPTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(sdk, 'CameraClient'), 'Public CameraClient is missing')
        self.control = ControlService(mode='disabled')
        self.addCleanup(self.control.close)
        detected = [dict(CATALOG['sources'][0], kind='sxw', pixel_format='MJPG'),
                    dict(CATALOG['sources'][0], id='camera-busy', available=False,
                         error='Camera is in use by another viewer', kind='sxw', pixel_format='MJPG')]

        def capture(options, frames, stop):
            frames.publish(JPEG)
            stop.wait()

        options = argparse.Namespace(source='v4l2', device='/dev/video4', wide_angle_host=None,
                                     depth=False, serial=None)
        self.controller = CaptureController(options, discover=lambda: detected, capture_fn=capture)
        self.server = CameraHTTPServer(0, self.controller.frames, controller=self.controller,
                                       bind='127.0.0.1', control=self.control)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.worker.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.controller.shutdown)
        self.client = sdk.CameraClient('http://127.0.0.1:{}'.format(self.server.server_port))

    def test_health_preserves_starting_stale_and_capture_error(self):
        for state in ('starting', 'stale', 'error'):
            with self.subTest(state=state):
                frames = LatestFrame('v4l2', stale_after=0 if state == 'stale' else 2)
                if state != 'starting':
                    frames.publish(JPEG)
                if state == 'error':
                    frames.fail('USB camera disconnected')
                self.controller.frames = frames
                health = self.client.health()
                self.assertEqual(health['status'], state)
                for method in (self.client.snapshot, self.client.stream):
                    with self.assertRaises(sdk.CameraFrameUnavailable) as failure:
                        method()
                    self.assertEqual(failure.exception.status, 503)
                    self.assertEqual(failure.exception.health['status'], state)
                    self.assertEqual(failure.exception.health['error'], health['error'])
                self.assertIsNone(self.client.sources()['selected'])

    def test_live_snapshot_is_original_jpeg_and_health_has_identity(self):
        self.controller.frames.identify('camera-one')
        self.controller.frames.publish(JPEG)
        self.assertEqual(self.client.snapshot(), JPEG)
        health = self.client.health()
        self.assertEqual(health['status'], 'live')
        self.assertEqual(health['source_id'], 'camera-one')
        self.assertGreaterEqual(health['frame_age_s'], 0)

    def test_source_selection_is_explicit_and_preserves_rejection_reason(self):
        catalog = self.client.sources()
        self.assertIsNone(catalog['selected'])
        self.assertEqual(catalog['sources'][1]['error'], 'Camera is in use by another viewer')
        selected = self.client.select_source(catalog['sources'][0]['id'])
        self.assertEqual(selected['selected'], 'camera-one')
        observer = sdk.CameraClient(self.client.url)
        self.assertEqual(observer.sources()['selected'], 'camera-one')
        with self.assertRaisesRegex(sdk.CameraClientError, 'Camera is in use by another viewer') as failure:
            self.client.select_source('camera-busy')
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(self.client.sources()['selected'], 'camera-one')
        with self.assertRaises(sdk.CameraClientError) as unknown:
            self.client.select_source('missing-id')
        self.assertEqual(unknown.exception.status, 400)

    def test_real_stream_yields_new_frames_and_ends_on_capture_error(self):
        self.controller.frames.publish(JPEG)
        with self.client.stream() as stream:
            self.assertEqual(next(stream), JPEG)
            second = JPEG + b'next-frame'
            self.controller.frames.publish(second)
            self.assertEqual(next(stream), second)
            self.controller.frames.fail('Disconnected')
            self.assertEqual(list(stream), [])
            self.assertTrue(stream.closed)
        self.assertEqual(self.client.health()['status'], 'error')

    def test_source_switch_ends_the_existing_stream(self):
        self.controller.frames.publish(JPEG)
        with self.client.stream() as stream:
            self.assertEqual(next(stream), JPEG)
            self.client.select_source('camera-one')
            self.assertEqual(list(stream), [])
        self.assertEqual(self.client.sources()['selected'], 'camera-one')


class CameraResponseTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(sdk, 'CameraClient'), 'Public CameraClient is missing')
        self.client = sdk.CameraClient('http://localhost:8080/', timeout=0.5)

    def response(self, body, content_type='application/json', **kwargs):
        response = Response(body, content_type, **kwargs)
        patcher = patch.object(self.client._opener, 'open', return_value=response)
        self.open = patcher.start()
        self.addCleanup(patcher.stop)
        return response

    def test_catalog_preserves_additive_fields_and_posts_only_explicit_id(self):
        catalog = copy.deepcopy(CATALOG)
        catalog['future'] = {'enabled': True}
        response = self.response(catalog)
        self.assertEqual(self.client.select_source('camera-one'), catalog)
        self.assertTrue(response.closed)
        request = self.open.call_args[0][0]
        self.assertEqual(request.full_url, 'http://localhost:8080/source')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data), {'id': 'camera-one'})
        self.assertEqual(self.open.call_count, 1)

    def test_json_validation_rejects_bad_fields_duplicate_keys_and_nonfinite_numbers(self):
        invalid = [b'[]', b'{}', b'{"sources":[],"selected":null,"selected":"x"}',
                   dict(CATALOG, selected=2), dict(CATALOG, sources={})]
        for field, value in (('id', 1), ('label', False), ('device', []), ('backend', 'future'),
                             ('available', 1), ('error', {})):
            catalog = copy.deepcopy(CATALOG)
            catalog['sources'][0][field] = value
            invalid.append(catalog)
        for value in invalid:
            with self.subTest(value=value):
                response = self.response(value)
                with self.assertRaises(sdk.CameraClientError):
                    self.client.sources()
                self.assertTrue(response.closed)
        for value in (dict(HEALTH, status='unknown'), dict(HEALTH, generation=True),
                      dict(HEALTH, frame_age_s=float('nan')), dict(HEALTH, generation=-1),
                      dict(HEALTH, frame_age_s=-1), dict(HEALTH, source_id=False)):
            with self.subTest(value=value):
                response = self.response(value)
                with self.assertRaises(sdk.CameraClientError):
                    self.client.health()
                self.assertTrue(response.closed)

    def test_json_and_snapshot_size_limits_close_responses(self):
        for body, content_type, method in (
                (b' ' * 65537, 'application/json', self.client.sources),
                (JPEG, 'image/jpeg', self.client.snapshot)):
            with self.subTest(content_type=content_type):
                self.client.max_frame_bytes = len(JPEG) - 1
                response = self.response(body, content_type)
                with self.assertRaises(sdk.CameraClientError):
                    method()
                self.assertTrue(response.closed)

    def test_snapshot_validates_mime_and_nonempty_body(self):
        for body, mime in ((b'', 'image/jpeg'), (b'<html>failure</html>', 'text/html')):
            response = self.response(body, mime)
            with self.assertRaises(sdk.CameraClientError) as failure:
                self.client.snapshot()
            self.assertEqual(failure.exception.status, 200)
            self.assertTrue(response.closed)

    def test_snapshot_and_json_reject_incomplete_content_length(self):
        for body, mime, method in ((JPEG, 'image/jpeg', self.client.snapshot),
                                   (CATALOG, 'application/json', self.client.sources)):
            response = self.response(body, mime)
            response.headers['Content-Length'] = str(len(response.getvalue()) + 1)
            with self.assertRaises(sdk.CameraClientError):
                method()
            self.assertTrue(response.closed)

    def test_short_reads_deliver_complete_snapshots_and_json(self):
        response = self.response(JPEG, 'image/jpeg', fragment=7)
        response.headers['Content-Length'] = str(len(JPEG))
        self.assertEqual(self.client.snapshot(), JPEG)
        self.assertTrue(response.closed)
        response = self.response(CATALOG, fragment=3)
        self.assertEqual(self.client.sources(), CATALOG)
        self.assertTrue(response.closed)

    def test_invalid_response_lengths_fail_without_reading_body(self):
        for lengths in (('invalid',), ('-1',), ('16777217',), ('1', '1')):
            response = self.response(JPEG, 'image/jpeg')
            for length in lengths:
                response.headers['Content-Length'] = length
            with patch.object(response, 'read', side_effect=AssertionError('Oversized body read')):
                with self.assertRaises(sdk.CameraClientError):
                    self.client.snapshot()
            self.assertTrue(response.closed)

    def test_503_frame_errors_are_typed_and_other_http_errors_keep_server_reason(self):
        for code, body, method, expected in (
                (503, HEALTH, self.client.snapshot, sdk.CameraFrameUnavailable),
                (503, HEALTH, self.client.stream, sdk.CameraFrameUnavailable),
                (409, {'error': 'Device is busy'}, lambda: self.client.select_source('camera-one'), sdk.CameraClientError),
                (503, {'error': 'Discovery failed'}, self.client.sources, sdk.CameraClientError)):
            with self.subTest(code=code, method=method):
                response = Response(body)
                error = HTTPError(self.client.url, code, 'Unavailable', response.headers, response)
                with patch.object(self.client._opener, 'open', side_effect=error) as open_request:
                    with self.assertRaises(expected) as failure:
                        method()
                    self.assertEqual(failure.exception.status, code)
                    self.assertTrue(response.closed)
                    self.assertEqual(open_request.call_count, 1)
                    if 'error' in body and body['error']:
                        self.assertIn(body['error'], str(failure.exception))

    def test_http_error_uses_its_own_code_on_python38(self):
        for code, body, method, expected in (
                (503, HEALTH, self.client.snapshot, sdk.CameraFrameUnavailable),
                (503, HEALTH, self.client.stream, sdk.CameraFrameUnavailable),
                (409, {'error': 'Busy'}, lambda: self.client.select_source('camera-one'), sdk.CameraClientError),
                (503, {'error': 'Discovery failed'}, self.client.sources, sdk.CameraClientError)):
            with self.subTest(code=code, method=method):
                response = Response(body)
                failure = LegacyHTTPError(self.client.url, code, 'Unavailable', response.headers, response)
                with patch.object(self.client._opener, 'open', side_effect=failure):
                    with self.assertRaises(expected) as caught:
                        method()
                self.assertEqual(caught.exception.status, code)
                self.assertTrue(response.closed)

    def test_protocol_mismatch_and_ambiguous_versions_close_response(self):
        for versions in ((), ('2',), ('1', '1'), ('1, 1',)):
            for method, mime, body in ((self.client.sources, 'application/json', CATALOG),
                                        (self.client.snapshot, 'image/jpeg', JPEG),
                                        (self.client.stream, 'multipart/x-mixed-replace; boundary=frame', multipart(JPEG))):
                response = self.response(body, mime, versions=versions)
                with self.assertRaises(sdk.CameraClientError):
                    method()
                self.assertTrue(response.closed)

    def test_network_failures_are_camera_errors_without_retry(self):
        for failure in (URLError('connection refused'), TimeoutError('timed out'), IncompleteRead(b'partial')):
            with patch.object(self.client._opener, 'open', side_effect=failure) as open_request:
                with self.assertRaises(sdk.CameraClientError):
                    self.client.snapshot()
                self.assertEqual(open_request.call_count, 1)

    def test_stream_handles_quoted_boundary_short_reads_and_binary_delimiter_bytes(self):
        frames = [JPEG, b'jpeg\r\n--other\r\n\x00\xffdata']
        response = self.response(multipart(*frames, boundary=b'other'),
                                 'multipart/x-mixed-replace; boundary="other"', fragment=3)
        with self.client.stream() as stream:
            self.assertEqual(list(stream), frames)
            self.assertTrue(stream.closed)
        self.assertTrue(response.closed)

    def test_stream_context_closes_on_early_break_exception_and_unused_stream(self):
        for action in ('break', 'exception', 'unused'):
            with self.subTest(action=action):
                response = self.response(multipart(JPEG, JPEG), 'multipart/x-mixed-replace; boundary=frame')
                try:
                    with self.client.stream() as stream:
                        if action == 'break':
                            for frame in stream:
                                self.assertEqual(frame, JPEG)
                                break
                        elif action == 'exception':
                            raise RuntimeError('consumer error')
                except RuntimeError:
                    self.assertEqual(action, 'exception')
                self.assertTrue(response.closed)
                self.assertTrue(stream.closed)
                stream.close()
                self.assertEqual(list(stream), [])

    def test_stream_setup_errors_close_response(self):
        for content_type in ('image/jpeg', 'multipart/x-mixed-replace',
                             'multipart/x-mixed-replace; boundary=""',
                             'multipart/x-mixed-replace; boundary=' + 'x' * 71):
            response = self.response(b'', content_type)
            with self.assertRaises(sdk.CameraClientError) as failure:
                self.client.stream()
            self.assertEqual(failure.exception.status, 200)
            self.assertTrue(response.closed)

    def test_malformed_truncated_and_oversized_stream_parts_close_response(self):
        for body in (
                b'', b'--wrong\r\n',
                b'--frame\r\n\r\n',
                b'--frame\r\nContent-Type: text/plain\r\nContent-Length: 1\r\n\r\nx\r\n',
                b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 0\r\n\r\n',
                b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: -1\r\n\r\n',
                b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 999999999\r\n\r\n',
                b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx\r\n',
                b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 10\r\n\r\nx',
                b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 1\r\n\r\nx!!',
                b'--frame\r\nX-Extra: ' + b'x' * 8192 + b'\r\n',
                b'--frame\r\n' + b'X-Extra: x\r\n' * 900):
            with self.subTest(body=body[:80]):
                response = self.response(body, 'multipart/x-mixed-replace; boundary=frame')
                with self.client.stream() as stream:
                    with self.assertRaises(sdk.CameraClientError) as failure:
                        next(stream)
                    self.assertEqual(failure.exception.status, 200)
                    self.assertTrue(stream.closed)
                self.assertTrue(response.closed)

    def test_io_failure_mid_stream_closes_response(self):
        response = self.response(multipart(JPEG), 'multipart/x-mixed-replace; boundary=frame')
        with self.client.stream() as stream:
            with patch.object(response, 'read', side_effect=TimeoutError('frame timeout')):
                with self.assertRaises(sdk.CameraClientError):
                    next(stream)
            self.assertTrue(response.closed)

    def test_invalid_configuration_and_source_id_fail_before_io(self):
        for url in ('file:///tmp/camera', 'http://user:secret@host', 'http://host/path',
                    'http://host/?q=x', 'http://host/#fragment', 'http://host/?', 'http://host/#'):
            with self.assertRaises(ValueError):
                sdk.CameraClient(url)
        for timeout in (0, -1, True, float('inf'), float('nan'), 11):
            with self.assertRaises(ValueError):
                sdk.CameraClient('http://localhost', timeout=timeout)
        for maximum in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                sdk.CameraClient('http://localhost', max_frame_bytes=maximum)
        with patch.object(self.client._opener, 'open') as open_request:
            for source_id in ('', 'x' * 129, 123):
                with self.assertRaises(ValueError):
                    self.client.select_source(source_id)
            open_request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
