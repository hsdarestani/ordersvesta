import base64
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault('BOTTOKEN', '123456:test-token')
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())

from app import media_upload
from app import media_bridge_recovery


class _FakeBridgeClient:
    def __init__(self):
        self.calls = []
        self.failed_chunk = False

    def _signed_get(self, op, payload=None):
        payload = dict(payload or {})
        self.calls.append((op, payload))
        if op == 'media_begin':
            return {'ready': True}
        if op == 'media_chunk':
            if payload['offset'] == 0 and not self.failed_chunk:
                self.failed_chunk = True
                raise RuntimeError('Signed GET Bridge ناموفق بود (media_chunk): timed out')
            return {'written': 1}
        if op == 'media_finish':
            return {'id': 42, 'source_url': 'https://example.test/image.jpg'}
        raise AssertionError(op)


class MediaUploadRecoveryTests(unittest.TestCase):
    def test_media_endpoints_prefer_direct_wordpress_before_worker(self):
        old = os.environ.get('BRIDGE_RELAY_URL')
        os.environ['BRIDGE_RELAY_URL'] = 'https://relay.example.test'
        try:
            client = type('Client', (), {'url': 'https://shop.example.test'})()
            endpoints = media_bridge_recovery.media_endpoints(client)
            self.assertEqual(endpoints[0][0], 'admin-ajax')
            self.assertEqual(endpoints[1][0], 'home')
            self.assertEqual(endpoints[-1][0], 'cloudflare-relay')
        finally:
            if old is None:
                os.environ.pop('BRIDGE_RELAY_URL', None)
            else:
                os.environ['BRIDGE_RELAY_URL'] = old

    def test_chunk_size_stays_below_proxy_request_line_risk(self):
        self.assertLessEqual(media_upload.CHUNK_SIZE, 3072)

    def test_transient_chunk_failure_retries_same_offset(self):
        client = _FakeBridgeClient()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'cover.jpg'
            path.write_bytes(os.urandom(media_upload.CHUNK_SIZE * 2 + 17))
            result = media_upload.upload_media(client, path, 'cover.jpg')

        self.assertEqual(result['id'], 42)
        chunk_calls = [payload for op, payload in client.calls if op == 'media_chunk']
        offsets = [payload['offset'] for payload in chunk_calls]
        self.assertGreaterEqual(offsets.count(0), 2)
        self.assertIn(media_upload.CHUNK_SIZE, offsets)
        self.assertIn(media_upload.CHUNK_SIZE * 2, offsets)
        for payload in chunk_calls:
            padded = payload['data'] + '=' * (-len(payload['data']) % 4)
            self.assertLessEqual(len(base64.urlsafe_b64decode(padded)), media_upload.CHUNK_SIZE)


if __name__ == '__main__':
    unittest.main()
