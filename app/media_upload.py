"""Bounded, resumable chunk transport for the signed WordPress bridge."""
import base64
import hashlib
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Shared across users and images: keep the WordPress worker pool responsive.
_REQUEST_SLOTS = threading.BoundedSemaphore(2)
CHUNK_SIZE = 5120
CHUNK_ATTEMPTS = 4


def _transient(exc):
    text = str(exc)
    return (
        isinstance(exc, (TimeoutError, ConnectionError, OSError))
        or bool(re.search(r'HTTP (?:408|429|5\d\d)\b', text))
        or 'Signed GET Bridge ناموفق بود' in text
        or 'موقتاً در دسترس نیست' in text
        or 'مسیر مستقیم Cutella پایدار نیست' in text
    )


def upload_media(client, path, filename):
    data = Path(path).read_bytes()
    if not data:
        raise RuntimeError('فایل تصویر خالی است.')
    upload_id = secrets.token_hex(16)

    def call(op, payload):
        with _REQUEST_SLOTS:
            return client._signed_get(op, payload)

    begin = call('media_begin', {
        'upload_id': upload_id,
        'filename': Path(filename or str(path)).name,
        'size': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
    })
    if begin.get('already_finished') and isinstance(begin.get('result'), dict):
        return begin['result']

    def send(offset):
        chunk = data[offset:offset + CHUNK_SIZE]
        payload = {
            'upload_id': upload_id,
            'offset': offset,
            'data': base64.urlsafe_b64encode(chunk).decode().rstrip('='),
        }
        for attempt in range(CHUNK_ATTEMPTS):
            try:
                # Retain upload ID and offset; the client signs each attempt
                # with a fresh nonce. Rewriting the same bytes is idempotent.
                return call('media_chunk', payload)
            except Exception as exc:
                if attempt == CHUNK_ATTEMPTS - 1 or not _transient(exc):
                    raise
                time.sleep(0.75 * (2 ** attempt))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(send, range(0, len(data), CHUNK_SIZE)))
    # Never restart a whole upload or retry attachment creation here.
    return call('media_finish', {'upload_id': upload_id})
