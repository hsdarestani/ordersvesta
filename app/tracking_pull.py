import base64
import hashlib
import hmac
import json
import re
import time
import zlib
from urllib.parse import parse_qs, urlparse

from app import operations as ops
from app import site_tracking_rows as rows_reader
from app import tracking_queue as tq
from app.public_media import BridgeHTTP

NONCE_RE = re.compile(r'^[a-f0-9]{20,64}$')
JOB_RE = re.compile(r'^[a-f0-9]{32}$')


def _b64url_decode(value):
    raw = str(value or '')
    raw += '=' * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(raw.encode())


def _json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Robots-Tag', 'noindex, nofollow')
    handler.end_headers()
    handler.wfile.write(body)


def _token():
    return (ops.cfg_get('bridge_token') or '').strip()


def _valid_common(query):
    token = _token()
    ts = (query.get('t') or [''])[0]
    nonce = (query.get('n') or [''])[0]
    sig = (query.get('s') or [''])[0].lower()
    if not token or not ts.isdigit() or abs(time.time() - int(ts)) > 300:
        return None
    if not NONCE_RE.fullmatch(nonce) or not re.fullmatch(r'[a-f0-9]{64}', sig):
        return None
    return token, ts, nonce, sig


class TrackingPullHTTP(BridgeHTTP):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/tracking-pull':
            return self._pull(parse_qs(parsed.query))
        if parsed.path == '/tracking-ack':
            return self._ack(parse_qs(parsed.query))
        return super().do_GET()

    def _pull(self, query):
        common = _valid_common(query)
        if not common:
            return _json(self, 401, {'success': False, 'error': 'unauthorized'})
        token, ts, nonce, sig = common
        expected = hmac.new(token.encode(), f'pull|{ts}|{nonce}'.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return _json(self, 401, {'success': False, 'error': 'bad signature'})

        job = tq.claim_for_site()
        if not job:
            return _json(self, 200, {'success': True, 'job': None})
        try:
            rows = rows_reader.read_tracking_rows(job['path'])
        except Exception as exc:
            tq.fail_job(job['id'], f'parse failed: {exc}')
            return _json(self, 422, {'success': False, 'error': str(exc)})

        return _json(self, 200, {
            'success': True,
            'job': {
                'id': job['id'],
                'filename': job['filename'],
                'rows': rows,
                'auto_match': True,
            },
        })

    def _ack(self, query):
        common = _valid_common(query)
        if not common:
            return _json(self, 401, {'success': False, 'error': 'unauthorized'})
        token, ts, nonce, sig = common
        job_id = (query.get('job') or [''])[0].lower()
        packed = (query.get('d') or [''])[0]
        if not JOB_RE.fullmatch(job_id) or not packed:
            return _json(self, 400, {'success': False, 'error': 'invalid ack'})
        expected = hmac.new(
            token.encode(),
            f'ack|{ts}|{nonce}|{job_id}|{packed}'.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return _json(self, 401, {'success': False, 'error': 'bad signature'})
        try:
            raw = zlib.decompress(_b64url_decode(packed))
            result = json.loads(raw.decode('utf-8'))
            if not isinstance(result, dict):
                raise ValueError('result must be an object')
        except Exception:
            return _json(self, 400, {'success': False, 'error': 'invalid result'})
        if not tq.complete_from_site(job_id, result):
            return _json(self, 404, {'success': False, 'error': 'job not found'})
        return _json(self, 200, {'success': True, 'ack': job_id})
