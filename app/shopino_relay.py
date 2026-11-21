"""Shopino tracking through the owner's Iran-hosted Vesta WordPress origin.

The foreign VPS cannot directly use Shopino's seller API: Shopino returns a
200 HTML "VPN را خاموش کنید" page instead of JSON. The existing WordPress
Bridge is HMAC-authenticated. A libsodium sealed box hides Shopino sessions,
phone numbers, OTPs and tracking codes even from intermediary URL logs.

Only a fixed allowlist of Shopino order and OTP operations is supported by the
server plugin. No general-purpose proxy and no third-party service.
"""
import base64
import json
import re
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
from nacl.public import PublicKey, SealedBox

from app import main as m
from app import bridge_client as bridge


_PUBLIC_KEY = None
_PUBLIC_KEY_LOCK = threading.Lock()
_SIGNED_GET_BEFORE_SHOPINO = bridge.BridgeWooClient._signed_get


def _shopino_signed_get(self, op, payload=None):
    if op != 'shopino_call':
        return _SIGNED_GET_BEFORE_SHOPINO(self, op, payload)
    # PATCH and OTP submissions must never be replayed after a timeout.
    endpoints = self._bridge_endpoints()
    if not endpoints:
        raise RuntimeError('مسیر ارتباط امن شاپینو در دسترس نیست.')
    name, endpoint = endpoints[0]
    params = self._signed_params(op, payload)
    try:
        response = self._stdlib_get(endpoint, params, 75.0)
        return self._decode(response)
    except Exception as exc:
        raise RuntimeError(
            'ارتباط شاپینو از مسیر امن سایت قطع شد. درخواست خودکار تکرار نشد '
            'تا کد یا عملیات تکراری ثبت نشود. '
            f'({type(exc).__name__}, {name})'
        ) from exc


bridge.BridgeWooClient._signed_get = _shopino_signed_get


def _get_public_key():
    global _PUBLIC_KEY
    if _PUBLIC_KEY is None:
        with _PUBLIC_KEY_LOCK:
            if _PUBLIC_KEY is None:
                result = bridge.BridgeWooClient().call('shopino_key')
                encoded = result.get('public_key')
                if not isinstance(encoded, str):
                    raise RuntimeError('کلید اتصال امن شاپینو در سایت آماده نیست.')
                key = base64.b64decode(encoded, validate=True)
                if len(key) != 32:
                    raise RuntimeError('کلید رمزگذاری شاپینو معتبر نیست.')
                _PUBLIC_KEY = PublicKey(key)
    return _PUBLIC_KEY


def _call_shopino(data):
    raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > 14000:
        raise ValueError('درخواست شاپینو بیش از حد بزرگ است.')
    ciphertext = SealedBox(_get_public_key()).encrypt(raw)
    sealed = base64.urlsafe_b64encode(ciphertext).rstrip(b'=').decode('ascii')
    result = bridge.BridgeWooClient().call('shopino_call', {'sealed': sealed})
    if not isinstance(result, dict):
        raise RuntimeError('پاسخ مسیر امن شاپینو معتبر نیست.')
    status = int(result.get('status_code') or 0)
    if status < 100 or status > 599:
        raise RuntimeError('پاسخ شاپینو فاقد کد وضعیت است.')
    return result


def _shopino_error(status):
    if status in (401, 403):
        raise m.AuthError('نشست شاپینو معتبر نیست. /login را بزنید.')
    if status >= 400:
        raise RuntimeError(f'شاپینو درخواست را رد کرد (HTTP {status}).')


class RelayShopino(m.Shopino):
    """Drop-in implementation of the existing authenticated Shopino client.

    runner.fetch_orders_for_rows calls client.c.request() and client.check(),
    while main.Shopino.probe/list/one/patch call client.c.get()/patch().
    Both interfaces stay unchanged.
    """

    def __init__(self, sid):
        self.sid = str(sid or '')
        self.c = self
        if not re.fullmatch(r'[A-Za-z0-9._~-]{8,512}', self.sid):
            raise m.AuthError('نشست شاپینو نامعتبر است. /login را بزنید.')

    def request(self, method, url, **kwargs):
        method = str(method).upper()
        parsed = urlsplit(str(url))
        base_path = f'/api/v1/shop-panel/shops/{m.SHOP}/order-shippings/'
        if parsed.scheme != 'https' or parsed.hostname != 'api.shopino.app':
            raise ValueError('Shopino URL is not allowlisted.')
        if parsed.path == base_path:
            if method != 'GET':
                raise ValueError('Only Shopino list GET is allowed here.')
            query = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
            query.update(kwargs.get('params') or {})
            page = int(query.get('page', 1))
            size = min(20, int(query.get('page_size', 20)))
            payload = {'action': 'orders_list', 'session': self.sid, 'page': page, 'page_size': size}
        else:
            match = re.fullmatch(re.escape(base_path) + r'([0-9]+)/', parsed.path)
            if not match:
                raise ValueError('Shopino request path is not allowlisted.')
            order_id = int(match.group(1))
            if method == 'GET':
                payload = {'action': 'orders_detail', 'session': self.sid, 'id': order_id}
            elif method == 'PATCH':
                values = kwargs.get('json') or {}
                if not isinstance(values, dict):
                    raise ValueError('Invalid tracking patch.')
                payload = {
                    'action': 'orders_patch', 'session': self.sid, 'id': order_id,
                    'tracking_code': str(values.get('tracking_code') or ''),
                    'type': str(values.get('type') or 'post'),
                }
            else:
                raise ValueError('Shopino method is not allowlisted.')
        result = _call_shopino(payload)
        status = int(result['status_code'])
        body = result.get('body') or {}
        return httpx.Response(status, json=body, request=httpx.Request(method, url))

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request('PATCH', url, **kwargs)


def send_verification_code(phone):
    phone = m.normalize_phone(phone)
    if not re.fullmatch(r'09\d{9}', phone):
        raise ValueError('شماره موبایل معتبر نیست؛ مثل 09123456789 وارد کنید.')
    result = _call_shopino({'action': 'send_code', 'phone': phone})
    _shopino_error(int(result['status_code']))
    return phone


def verify_phone(phone, code):
    phone = m.normalize_phone(phone)
    code = re.sub(r'\D', '', str(code or '').translate(m.DIGITS))
    if not re.fullmatch(r'09\d{9}', phone) or not re.fullmatch(r'\d{4,8}', code):
        raise ValueError('شماره یا کد تأیید معتبر نیست.')
    result = _call_shopino({'action': 'verify_code', 'phone': phone, 'code': code})
    _shopino_error(int(result['status_code']))
    session = result.get('sessionid')
    if not session:
        raise m.AuthError('ورود تأیید شد اما نشست شاپینو دریافت نشد.')
    count = RelayShopino(session).probe()
    return session, count


# Preserve all Telegram commands and matching logic; swap transport only.
m.Shopino = RelayShopino
m.send_verification_code = send_verification_code
m.verify_phone = verify_phone
