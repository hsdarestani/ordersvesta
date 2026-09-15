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

# 3 KiB keeps the signed request comfortably below common proxy/WAF request-line
# limits after base64 + JSON + HMAC parameters are added. The previous 5 KiB chunks
# were too close to the 8 KiB boundary and could stall on the media relay path.
CHUNK_SIZE = 3072
CHUNK_ATTEMPTS = 5
BEGIN_ATTEMPTS = 3


def _transient(exc):
    text = str(exc).lower()
    return (
        isinstance(exc, (TimeoutError, ConnectionError, OSError))
        or bool(re.search(r'http (?:408|429|5\d\d)\b', text))
        or 'signed get bridge ناموفق بود' in text
        or 'موقتاً در دسترس نیست' in text
        or 'مسیر مستقیم cutella پایدار نیست' in text
        or 'timeout' in text
        or 'timed out' in text
    )


def upload_media(client, path, filename):
    data = Path(path).read_bytes()
    if not data:
        raise RuntimeError('فایل تصویر خالی است.')
    upload_id = secrets.token_hex(16)

    def call(op, payload):
        with _REQUEST_SLOTS:
            return client._signed_get(op, payload)

    def call_with_retry(op, payload, attempts, base_delay=0.6):
        last = None
        for attempt in range(attempts):
            try:
                return call(op, payload)
            except Exception as exc:
                last = exc
                if attempt == attempts - 1 or not _transient(exc):
                    raise
                time.sleep(base_delay * (2 ** attempt))
        raise last

    # media_begin only truncates/initializes the temporary upload and is safe to
    # retry before any chunks are sent.
    begin = call_with_retry('media_begin', {
        'upload_id': upload_id,
        'filename': Path(filename or str(path)).name,
        'size': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
    }, BEGIN_ATTEMPTS)
    if begin.get('already_finished') and isinstance(begin.get('result'), dict):
        return begin['result']

    def send(offset):
        chunk = data[offset:offset + CHUNK_SIZE]
        payload = {
            'upload_id': upload_id,
            'offset': offset,
            'data': base64.urlsafe_b64encode(chunk).decode().rstrip('='),
        }
        # Rewriting the same upload_id + offset is idempotent, so transient
        # transport failures can be retried without duplicating media.
        return call_with_retry('media_chunk', payload, CHUNK_ATTEMPTS, base_delay=0.75)

    # Two in-flight chunks keep uploads reasonably fast without flooding the
    # WordPress/PHP worker pool or tripping edge rate limits.
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(send, range(0, len(data), CHUNK_SIZE)))

    # Do not retry attachment creation here. The transport gives media_finish a
    # long timeout; retrying after an ambiguous timeout could create duplicates.
    return call('media_finish', {'upload_id': upload_id})
