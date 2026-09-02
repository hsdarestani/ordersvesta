import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import media_upload as media


class ChunkUploadTests(unittest.TestCase):
    def test_lost_chunk_response_retries_same_session_and_bytes(self):
        data = bytes(range(256)) * 70
        calls, received = [], {}
        lost = False

        class Client:
            def _signed_get(self, op, payload):
                nonlocal lost
                calls.append((op, dict(payload)))
                if op == 'media_chunk':
                    encoded = payload['data']
                    received[payload['offset']] = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
                    if payload['offset'] == media.CHUNK_SIZE and not lost:
                        lost = True
                        raise RuntimeError('Bridge HTTP 503: temporarily unavailable')
                if op == 'media_finish':
                    self_data = b''.join(received[x] for x in sorted(received))
                    assert self_data == data
                    return {'id': 42}
                return {}

        with tempfile.TemporaryDirectory() as directory, patch.object(media.time, 'sleep'):
            path = Path(directory) / 'image.jpg'
            path.write_bytes(data)
            self.assertEqual(media.upload_media(Client(), path, path.name), {'id': 42})
        self.assertEqual(sum(op == 'media_begin' for op, _ in calls), 1)
        self.assertEqual(sum(op == 'media_finish' for op, _ in calls), 1)
        self.assertEqual(len({p['upload_id'] for _, p in calls}), 1)
        retries = [p for op, p in calls if op == 'media_chunk' and p['offset'] == media.CHUNK_SIZE]
        self.assertEqual(len(retries), 2)
        self.assertEqual(retries[0], retries[1])
        self.assertEqual(calls[0][1]['sha256'], hashlib.sha256(data).hexdigest())

    def test_permanent_failure_does_not_retry_or_finalize(self):
        self._assert_failure('Bridge HTTP 400: Invalid media chunk.', 1)

    def test_transient_failure_has_bounded_retries_and_no_finalize(self):
        self._assert_failure('Signed GET Bridge ناموفق بود: timeout', media.CHUNK_ATTEMPTS)

    def _assert_failure(self, error, attempts):
        calls = []
        class Client:
            def _signed_get(self, op, payload):
                calls.append(op)
                if op == 'media_chunk':
                    raise RuntimeError(error)
                return {}
        with tempfile.TemporaryDirectory() as directory, patch.object(media.time, 'sleep'):
            path = Path(directory) / 'image.jpg'
            path.write_bytes(b'image')
            with self.assertRaises(RuntimeError):
                media.upload_media(Client(), path, path.name)
        self.assertEqual(calls.count('media_chunk'), attempts)
        self.assertNotIn('media_finish', calls)

    def test_finished_session_does_not_upload_again(self):
        class Client:
            def _signed_get(self, op, payload):
                assert op == 'media_begin'
                return {'already_finished': True, 'result': {'id': 5}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.jpg'
            path.write_bytes(b'image')
            self.assertEqual(media.upload_media(Client(), path, path.name), {'id': 5})
