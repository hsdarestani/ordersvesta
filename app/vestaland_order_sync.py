import hashlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import urlparse

from app.bridge_client import BridgeWooClient
from app.tracking_telemetry import TrackingPullTelemetryHTTP

STORE = 'vesta'
HAMOON_STATUS = 'https://pay.hamooncloud.ir/payments/vestaland-market/status'
MAX_BODY = 128 * 1024
SSL = ssl.create_default_context()
ADDRESS_KEYS = ('first_name','last_name','company','address_1','address_2','city','state','postcode','country','email','phone')


def _normal_items(items):
    output = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue
        pid = int(raw.get('id') or 0)
        qty = max(1, min(20, int(raw.get('quantity') or 1)))
        price = int(raw.get('price_toman') or 0)
        line = int(raw.get('line_total_toman') or price * qty)
        output.append({
            'id': pid,
            'parent_id': int(raw.get('parent_id') or 0) or None,
            'quantity': qty,
            'price_toman': price,
            'line_total_toman': line,
        })
    return output


def _normal_address(address):
    address = address if isinstance(address, dict) else {}
    out = {key: str(address.get(key) or '').strip() for key in ADDRESS_KEYS}
    out['country'] = 'IR'
    return out


def payload_hash(store, amount_toman, items, address):
    payload = {
        'store': str(store),
        'amount_toman': int(amount_toman),
        'items': _normal_items(items),
        'address': _normal_address(address),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def hamoon_status(receipt):
    url = HAMOON_STATUS + '?' + urllib.parse.urlencode({'receipt': receipt})
    req = urllib.request.Request(url, headers={
        'Accept': 'application/json',
        'Cache-Control': 'no-cache',
        'User-Agent': 'OrdersVestaVestalandSync/1.0',
    })
    try:
        with urllib.request.urlopen(req, timeout=15, context=SSL) as res:
            return json.loads(res.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raise ValueError(f'Hamoon status HTTP {exc.code}') from exc
    except Exception as exc:
        raise ValueError(f'Hamoon status failed: {exc}') from exc


class VestalandOrderSyncHTTP(TrackingPullTelemetryHTTP):
    def _json(self, status, payload):
        raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if urlparse(self.path).path.rstrip('/') == '/vestaland-order-sync/health':
            return self._json(200, {'ok': True, 'service': 'ordersvesta-vestaland-order-sync', 'store': STORE})
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path.rstrip('/') != '/vestaland-order-sync':
            return self._json(404, {'ok': False, 'error': 'NOT_FOUND'})
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0 or length > MAX_BODY:
                raise ValueError('Invalid body size.')
            body = json.loads(self.rfile.read(length).decode('utf-8'))
            if not isinstance(body, dict) or str(body.get('store') or '') != STORE:
                raise ValueError('Store mismatch.')

            receipt = str(body.get('receipt') or '').strip().lower()
            intent = str(body.get('intent') or '').strip()
            amount = int(body.get('amount_toman') or 0)
            items = _normal_items(body.get('items'))
            address = _normal_address(body.get('address'))
            supplied_hash = str(body.get('payload_hash') or '').strip().lower()
            local_hash = payload_hash(STORE, amount, items, address)
            if len(receipt) < 24 or len(intent) < 20 or amount < 1000 or not items:
                raise ValueError('Incomplete paid order proof.')
            if supplied_hash != local_hash:
                raise ValueError('Payload hash mismatch.')

            proof = hamoon_status(receipt)
            if not proof.get('ok') or proof.get('status') != 'paid':
                raise ValueError('Hamoon payment is not paid.')
            if str(proof.get('intent') or '') != intent:
                raise ValueError('Hamoon intent mismatch.')
            if str(proof.get('plan') or '') != STORE:
                raise ValueError('Hamoon store mismatch.')
            if int(proof.get('amount_toman') or 0) != amount:
                raise ValueError('Hamoon amount mismatch.')
            if str(proof.get('metadata_hash') or '').lower() != local_hash:
                raise ValueError('Hamoon payload hash mismatch.')

            result = BridgeWooClient().call('create_paid_order', {
                'receipt': receipt,
                'intent': intent,
                'amount_toman': amount,
                'payload_hash': local_hash,
                'items': items,
                'address': address,
            })
            return self._json(200, {
                'ok': True,
                'store': STORE,
                'receipt': receipt,
                'intent': intent,
                'order_id': int(result.get('order_id') or 0),
                'status': str(result.get('status') or ''),
                'already_exists': bool(result.get('already_exists')),
            })
        except ValueError as exc:
            return self._json(422, {'ok': False, 'error': str(exc)})
        except Exception as exc:
            return self._json(502, {'ok': False, 'error': str(exc)[:500]})
