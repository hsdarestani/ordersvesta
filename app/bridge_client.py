import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
import zlib
from pathlib import Path

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


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


class BridgeWooClient:
    def __init__(self):
        self.url = (ops.cfg_get('url') or '').rstrip('/')
        self.token = ops.cfg_get('bridge_token') or ''
        if not self.url or not self.token:
            raise RuntimeError('Bridge ووکامرس تنظیم نشده است. از «🔌 اتصال ووکامرس» استفاده کنید.')
        self.c = httpx.Client(
            timeout=httpx.Timeout(180.0, connect=25.0),
            follow_redirects=True,
            headers=BROWSER_HEADERS,
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
        # Every transport attempt gets a fresh nonce/signature. Chunk writes are
        # offset-based/idempotent, so retrying the same chunk is safe.
        for label, endpoint in (
            ('home', f'{self.url}/'),
            ('index', f'{self.url}/index.php'),
        ):
            params = self._signed_params(op, payload)
            try:
                r = self.c.get(endpoint, params=params)
                return self._decode(r)
            except Exception as exc:
                errors.append(f'{label}: {exc}')
        raise RuntimeError('Signed GET Bridge ناموفق بود: ' + ' | '.join(errors[-2:]))

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
        ops.clear_state(uid)
        try:
            await update.message.delete()
        except Exception:
            pass
        try:
            total = await asyncio.to_thread(BridgeWooClient().probe)
            return await update.effective_chat.send_message(
                f'✅ Vesta Bot Bridge متصل شد. {total} محصول قابل مشاهده است.\n\n'
                'Transport: Signed GET + HMAC v2',
                reply_markup=ops.woo_menu(),
            )
        except Exception as exc:
            try:
                diag = await asyncio.to_thread(BridgeWooClient().diagnostics)
            except Exception:
                diag = 'diagnostic unavailable'
            return await update.effective_chat.send_message(
                f'⚠️ اطلاعات Bridge ذخیره شد ولی تست اتصال موفق نبود:\n{exc}\n\n'
                f'تست دسترسی عمومی از سرور ربات: {diag}',
                reply_markup=ops.woo_menu(),
            )

    return await update.message.reply_text('برای اتصال Bridge مرحله فعلی را کامل کنید.')


ops.WooClient = BridgeWooClient
ops.begin_woo_setup = begin_woo_setup
ops.setup_text = setup_text
