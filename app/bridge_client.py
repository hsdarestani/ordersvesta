import asyncio
import json
import mimetypes
from pathlib import Path

import httpx

from app import operations as ops


class BridgeWooClient:
    def __init__(self):
        self.url = (ops.cfg_get('url') or '').rstrip('/')
        self.token = ops.cfg_get('bridge_token') or ''
        if not self.url or not self.token:
            raise RuntimeError('Bridge ووکامرس تنظیم نشده است. از «🔌 اتصال ووکامرس» استفاده کنید.')
        self.c = httpx.Client(timeout=httpx.Timeout(90.0, connect=25.0), follow_redirects=True)

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

    def _request_once(self, endpoint, op, payload=None, file_path=None, filename=None):
        form = {
            'op': op,
            'token': self.token,
            'payload': json.dumps(payload or {}, ensure_ascii=False),
        }
        files = None
        handle = None
        try:
            if file_path:
                handle = open(file_path, 'rb')
                mime = mimetypes.guess_type(filename or str(file_path))[0] or 'application/octet-stream'
                files = {'file': (filename or Path(file_path).name, handle, mime)}
            response = self.c.post(endpoint, data=form, files=files)
            return self._decode(response)
        finally:
            if handle:
                handle.close()

    def call(self, op, payload=None, file_path=None, filename=None):
        # First use the lightweight public bridge route. admin-ajax is a fallback for
        # WordPress setups where template routing is altered by the theme/cache layer.
        endpoints = [
            f'{self.url}/?vesta_bot_bridge=1',
            f'{self.url}/wp-admin/admin-ajax.php?action=vesta_bot_bridge',
        ]
        errors = []
        for endpoint in endpoints:
            try:
                return self._request_once(endpoint, op, payload, file_path, filename)
            except Exception as exc:
                errors.append(str(exc))
        raise RuntimeError('اتصال به Vesta Bot Bridge ناموفق بود: ' + ' | '.join(errors[-2:]))

    def probe(self):
        return self.call('ping').get('product_count', '?')

    def categories(self):
        return self.call('categories').get('categories', [])

    def recent_products(self):
        return self.call('recent_products').get('products', [])

    def upload_media(self, path, filename):
        return self.call('upload_media', file_path=path, filename=filename)

    def create_product(self, data):
        return self.call('create_product', payload=data)

    def create_variation(self, product_id, payload):
        return self.call('create_variation', payload={
            'product_id': int(product_id),
            'variation': payload,
        })


async def begin_woo_setup(update):
    uid = update.effective_user.id
    ops.set_state(uid, 'woo_setup', 'url', {})
    await update.message.reply_text(
        '🔌 اتصال ووکامرس با Vesta Bot Bridge\n\n'
        'اول افزونه Vesta Bot Bridge را روی سایت نصب و فعال کنید.\n'
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
                'از این به بعد Consumer Key / Secret و Application Password لازم نیست.',
                reply_markup=ops.woo_menu(),
            )
        except Exception as exc:
            return await update.effective_chat.send_message(
                f'⚠️ اطلاعات Bridge ذخیره شد ولی تست اتصال موفق نبود:\n{exc}\n\n'
                'مطمئن شوید افزونه فعال است و توکن را درست کپی کرده‌اید.',
                reply_markup=ops.woo_menu(),
            )

    return await update.message.reply_text('برای اتصال Bridge مرحله فعلی را کامل کنید.')


# Replace WooCommerce networking and connection flow globally. The existing product,
# category, photo and variation workflows continue to use ops.WooClient at runtime.
ops.WooClient = BridgeWooClient
ops.begin_woo_setup = begin_woo_setup
ops.setup_text = setup_text
