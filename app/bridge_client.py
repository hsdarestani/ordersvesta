import asyncio
import base64
import hashlib
import hmac
import http.client
import json
import os
import secrets
import ssl
import time
import zlib
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from app import operations as ops

BROWSER_HEADERS = {
    'accept': 'application/json,text/plain,*/*',
    'accept-language': 'en-US,en;q=0.9',
    'cache-control': 'no-cache',
    'pragma': 'no-cache',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36',
}

# Keep signed GET URLs comfortably below common proxy/WAF request-line limits.
MEDIA_CHUNK_SIZE = 3072
BRIDGE_TIMEOUT = httpx.Timeout(connect=12.0, read=10.0, write=10.0, pool=5.0)
BRIDGE_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)
TRANSIENT_ERRORS = (
    asyncio.TimeoutError,
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

# BridgeWooClient instances are intentionally cheap, but the underlying transports
# are shared. The Iran-hosted shop has a slow TLS cold start; throwing the pool away
# on every Telegram action made every action pay that handshake cost again.
_SYNC_CLIENT = httpx.Client(
    timeout=BRIDGE_TIMEOUT,
    follow_redirects=True,
    headers=BROWSER_HEADERS,
    limits=BRIDGE_LIMITS,
    trust_env=False,
)
_ASYNC_CLIENT = httpx.AsyncClient(
    timeout=BRIDGE_TIMEOUT,
    follow_redirects=True,
    headers=BROWSER_HEADERS,
    limits=BRIDGE_LIMITS,
    trust_env=False,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


class _StdlibResponse:
    def __init__(self, status_code, headers, body):
        self.status_code = int(status_code)
        self.headers = headers
        self._body = body

    @property
    def text(self):
        return self._body.decode('utf-8', errors='replace')

    def json(self):
        return json.loads(self.text)


class BridgeWooClient:
    def __init__(self):
        self.url = (ops.cfg_get('url') or '').rstrip('/')
        self.bridge_url = (os.getenv('BRIDGE_RELAY_URL') or self.url).rstrip('/')
        self.token = ops.cfg_get('bridge_token') or ''
        if not self.url or not self.token:
            raise RuntimeError('Bridge ووکامرس تنظیم نشده است. از «🔌 اتصال ووکامرس» استفاده کنید.')
        self.c = _SYNC_CLIENT
        self.ac = _ASYNC_CLIENT

    def _bridge_endpoints(self):
        # Resolve lazily as well as in __init__. This keeps requests safe when a
        # long-lived/cached instance was created before BRIDGE_RELAY_URL support
        # was deployed and therefore has no bridge_url attribute yet.
        bridge_url = getattr(
            self,
            'bridge_url',
            (os.getenv('BRIDGE_RELAY_URL') or self.url).rstrip('/'),
        )
        if bridge_url != self.url:
            # The Worker always forwards to the origin Bridge root. A single
            # attempt avoids sending the same signed operation three times.
            return (('cloudflare-relay', f'{bridge_url}/'),)
        return (
            ('admin-ajax', f'{self.url}/wp-admin/admin-ajax.php'),
            ('home', f'{self.url}/'),
            ('index', f'{self.url}/index.php'),
        )

    def _decode(self, response):
        ctype = (response.headers.get('content-type') or '').lower()
        if response.status_code == 403 and 'application/json' not in ctype:
            raise RuntimeError('Bridge توسط وب‌سرور با 403 مسدود شد.')
        try:
            body = response.json()
        except Exception:
            snippet = (response.text or '')[:250].replace('\n', ' ')
            raise RuntimeError(f'پاسخ Bridge معتبر نیست (HTTP {response.status_code}): {snippet}')
        if response.status_code >= 400 or not body.get('success'):
            data = body.get('data') if isinstance(body, dict) else None
            if isinstance(data, dict):
                detail = data.get('message') or str(data)
            else:
                detail = str(data or body)
            raise RuntimeError(f'Bridge HTTP {response.status_code}: {detail}')
        data = body.get('data')
        return data if isinstance(data, dict) else {}

    def _stdlib_get(self, endpoint, params, timeout):
        """Fresh stdlib TLS transport.

        Production diagnostics prove raw Python TLS reaches the Iranian host in
        ~0.2s while httpx intermittently stalls in its SSL transport. A fresh
        HTTPSConnection also avoids reusing a poisoned/stale pooled socket after
        the earlier WAF throttling incident.
        """
        target = urlsplit(endpoint)
        if target.scheme != 'https' or not target.hostname:
            raise RuntimeError('Bridge URL must use a valid HTTPS address.')
        path = target.path or '/'
        query = urlencode(params)
        if target.query:
            query = f'{target.query}&{query}'
        if query:
            path = f'{path}?{query}'

        connection = http.client.HTTPSConnection(
            target.hostname,
            target.port or 443,
            timeout=max(0.1, float(timeout)),
            context=ssl.create_default_context(),
        )
        try:
            connection.request('GET', path, headers=BROWSER_HEADERS)
            raw_response = connection.getresponse()
            raw_body = raw_response.read()
            return _StdlibResponse(raw_response.status, raw_response.headers, raw_body)
        except (TimeoutError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
            raise httpx.NetworkError(str(exc)) from exc
        finally:
            connection.close()

    def _signed_params(self, op, payload=None):
        raw = json.dumps(payload or {}, ensure_ascii=False, separators=(',', ':')).encode()
        packed = _b64url(zlib.compress(raw, 9)) if payload else ''
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        message = f'v2|{ts}|{nonce}|{op}|{packed}'
        sig = hmac.new(self.token.encode(), message.encode(), hashlib.sha256).hexdigest()
        return {'vbb': '2', 't': ts, 'n': nonce, 'o': op, 'd': packed, 's': sig}

    def _signed_get(self, op, payload=None):
        errors = []
        deadline = time.monotonic() + 15.0
        # Every transport attempt gets a fresh nonce/signature. Chunk writes are
        # offset-based/idempotent, so retrying the same chunk is safe.
        for label, endpoint in self._bridge_endpoints():
            params = self._signed_params(op, payload)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                # Reserve time for the fallback endpoint instead of spending the
                # whole retry budget on the same WordPress rewrite route.
                r = self._stdlib_get(endpoint, params, min(12.0, remaining))
                return self._decode(r)
            except TRANSIENT_ERRORS as exc:
                errors.append(f'{label}: {exc}')
            except RuntimeError:
                raise
            except Exception as exc:
                errors.append(f'{label}: {exc}')
        raise RuntimeError('Signed GET Bridge ناموفق بود: ' + ' | '.join(errors[-2:]))

    async def _async_signed_get(self, op, payload=None):
        """Async Bridge transport for handlers; all retries share a 15-second budget."""
        errors = []
        deadline = time.monotonic() + 15.0
        for label, endpoint in self._bridge_endpoints():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            params = self._signed_params(op, payload)
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._stdlib_get, endpoint, params, min(12.0, remaining)
                    ),
                    timeout=min(12.0, remaining),
                )
                return self._decode(response)
            except TRANSIENT_ERRORS as exc:
                errors.append(f'{label}: {exc}')
            except RuntimeError:
                raise
            except Exception as exc:
                errors.append(f'{label}: {exc}')
        raise RuntimeError('Signed GET Bridge ناموفق بود: ' + ' | '.join(errors[-2:]))

    async def aclose(self):
        # Shared pools live for the bot process lifetime.
        return None

    async def aprobe(self):
        return (await self._async_signed_get('ping')).get('product_count', '?')

    async def acategories(self):
        return (await self._async_signed_get('categories')).get('categories', [])

    async def arecent_products(self):
        return (await self._async_signed_get('recent_products')).get('products', [])

    def call(self, op, payload=None, file_path=None, filename=None):
        if file_path:
            return self.upload_media(file_path, filename or Path(file_path).name)
        return self._signed_get(op, payload)

    def probe(self):
        return self._signed_get('ping').get('product_count', '?')

    def categories(self):
        return self._signed_get('categories').get('categories', [])

    def recent_products(self):
        return self._signed_get('recent_products').get('products', [])

    def upload_media(self, path, filename):
        # Do not ask WordPress/Zhaket Booster to download from an external domain.
        # Transfer the image itself through small HMAC-signed GET chunks, then let
        # WordPress assemble and sideload it locally into Media Library.
        data = Path(path).read_bytes()
        if not data:
            raise RuntimeError('فایل تصویر خالی است.')

        upload_id = secrets.token_hex(16)
        digest = hashlib.sha256(data).hexdigest()
        safe_name = Path(filename or str(path)).name or 'vesta-product.jpg'

        begin = self._signed_get('media_begin', {
            'upload_id': upload_id,
            'filename': safe_name,
            'size': len(data),
            'sha256': digest,
        })
        if begin.get('already_finished') and isinstance(begin.get('result'), dict):
            return begin['result']

        for offset in range(0, len(data), MEDIA_CHUNK_SIZE):
            chunk = data[offset:offset + MEDIA_CHUNK_SIZE]
            self._signed_get('media_chunk', {
                'upload_id': upload_id,
                'offset': offset,
                'data': _b64url(chunk),
            })

        return self._signed_get('media_finish', {'upload_id': upload_id})

    def create_product(self, data):
        return self._signed_get('create_product', data)

    def create_variation(self, product_id, payload):
        return self._signed_get('create_variation', {
            'product_id': int(product_id),
            'variation': payload,
        })

    def diagnostics(self):
        out = []
        for label, url in (
            ('home', f'{self.url}/'),
            ('wp-json', f'{self.url}/wp-json/'),
        ):
            try:
                r = self.c.get(url)
                out.append(f'{label}:{r.status_code}')
            except Exception as exc:
                out.append(f'{label}:{type(exc).__name__}')
        return ', '.join(out)


async def begin_woo_setup(update):
    uid = update.effective_user.id
    ops.set_state(uid, 'woo_setup', 'url', {})
    await update.message.reply_text(
        '🔌 اتصال ووکامرس با Vesta Bot Bridge\n\n'
        'نسخه جدید افزونه Vesta Bot Bridge را روی سایت فعال کنید.\n'
        'بعد آدرس سایت را بفرستید؛ مثال:\nhttps://vesta-cosmetics.ir',
        reply_markup=ops.cancel_menu(),
    )


async def setup_text(update, state):
    uid = update.effective_user.id
    value = (update.message.text or '').strip()
    data = state['data']
    step = state['step']

    if step == 'url':
        if not value.startswith('http://') and not value.startswith('https://'):
            return await update.message.reply_text('آدرس باید با http:// یا https:// شروع شود.')
        data['url'] = value.rstrip('/')
        ops.update_state(uid, step='bridge_token', data=data)
        return await update.message.reply_text(
            'حالا Bridge Token را بفرستید.\n\n'
            'داخل وردپرس: WooCommerce → Vesta Bot Bridge → Bridge Token'
        )

    if step == 'bridge_token':
        token = value.strip()
        if len(token) < 24:
            return await update.message.reply_text('Bridge Token معتبر به نظر نمی‌رسد. دوباره کپی و ارسال کنید.')
        ops.cfg_set('url', data['url'])
        ops.cfg_set('bridge_token', token)
        ops.reset_user_flow(uid)
        try:
            await update.message.delete()
        except Exception:
            pass
        status = await update.effective_chat.send_message(
            '⏳ اطلاعات ذخیره شد؛ اتصال Bridge در پس‌زمینه بررسی می‌شود…',
            reply_markup=ops.woo_menu(),
        )

        async def verify_bridge():
            client = BridgeWooClient()
            try:
                total = await client.aprobe()
                await status.edit_text(
                    f'✅ Vesta Bot Bridge متصل شد. {total} محصول قابل مشاهده است.\n\n'
                    'Transport: Async Signed GET + HMAC v2'
                )
            except Exception as exc:
                await status.edit_text(f'⚠️ اطلاعات ذخیره شد ولی تست اتصال موفق نبود:\n{exc}')
            finally:
                await client.aclose()

        asyncio.create_task(verify_bridge())
        return None

    return await update.message.reply_text('برای اتصال Bridge مرحله فعلی را کامل کنید.')


ops.WooClient = BridgeWooClient
ops.begin_woo_setup = begin_woo_setup
ops.setup_text = setup_text
