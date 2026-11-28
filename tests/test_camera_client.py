"""Camera SDK contracts over real loopback HTTP and controlled byte streams."""
import argparse
from http.client import HTTPMessage
import io
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import lite3_dev_server as sdk
from src.camera import CaptureController, LatestFrame
from src.control import ControlService
from src.http_server import CameraHTTPServer


JPEG = (Path(__file__).parent / 'fixtures/camera-frame.jpg').read_bytes()
CATALOG = {'sources': [{'id': 'camera-one', 'label': 'Camera one', 'device': '/dev/video4',
                        'backend': 'v4l2', 'available': True, 'error': None}], 'selected': None}


class Response(io.BytesIO):
    def __init__(self, body, content_type):
        super().__init__(body)
        self.headers = HTTPMessage()
        self.headers['Content-Type'] = content_type
        self.headers['X-Lite3-Camera-Protocol'] = '1'
        self.status = 200


def multipart(*frames, boundary=b'frame'):
    return b''.join(b'--' + boundary + b'\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                    str(len(frame)).encode() + b'\r\n\r\n' + frame + b'\r\n' for frame in frames) + \
        b'--' + boundary + b'--\r\n'


class CameraHTTPTests(unittest.TestCase):
    def setUp(self):
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


class CameraResponseTests(unittest.TestCase):
    def test_stream_context_closes_on_early_break_exception_and_unused_stream(self):
        client = sdk.CameraClient('http://localhost:8080/', timeout=0.5)
        for action in ('break', 'exception', 'unused'):
            with self.subTest(action=action):
                response = Response(multipart(JPEG, JPEG), 'multipart/x-mixed-replace; boundary=frame')
                try:
                    with patch.object(client._opener, 'open', return_value=response), client.stream() as stream:
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


if __name__ == '__main__':
    unittest.main()
