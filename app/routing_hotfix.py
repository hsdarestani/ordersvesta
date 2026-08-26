import asyncio
import os
import time
from pathlib import Path

from app import bridge_client as bridge
from app import main as m
from app import operations as ops
from app import site_tracking as st

# Capture the fully composed routers before applying the final production fixes.
ORIG_TEXT = st.text
ORIG_DOCUMENT = st.document

_LAST_PRODUCT_START = {}


# ---------------------------------------------------------------------------
# Woo product creation must never wait for a Bridge health check.
# ---------------------------------------------------------------------------
async def begin_product_fast(update):
    """Start the product wizard from local config only.

    A remote ping here made a basic Telegram button depend on the Iran-hosted
    WordPress/Cloudflare path. Real Bridge access is only required later when
    categories/media/publish are actually used.
    """
    uid = update.effective_user.id
    now = time.monotonic()
    last = _LAST_PRODUCT_START.get(uid, 0.0)
    if now - last < 0.8:
        return
    _LAST_PRODUCT_START[uid] = now

    ops.reset_user_flow(uid)
    site_url = (ops.cfg_get('url') or '').strip()
    token = (ops.cfg_get('bridge_token') or '').strip()
    if not site_url or not token:
        return await update.message.reply_text(
            '❌ اتصال ووکامرس هنوز تنظیم نشده است. ابتدا «🔌 اتصال ووکامرس» را انجام دهید.',
            reply_markup=ops.woo_menu(),
        )

    ops.set_state(
        uid,
        'woo_product',
        'name',
        {'images': [], 'categories': [], 'category_page': 0},
    )
    return await update.message.reply_text(
        '➕ ثبت محصول جدید\n\nاسم محصول را بفرستید.',
        reply_markup=ops.cancel_menu(),
    )


# operations.text resolves this module global at runtime.
ops.begin_product = begin_product_fast


# ---------------------------------------------------------------------------
# Separate Shopino tracking and website tracking explicitly.
# ---------------------------------------------------------------------------
async def text(update, ctx):
    if not await m.access(update):
        return

    uid = update.effective_user.id
    value = (update.message.text or '').strip()

    # Merely entering the website-tracking section is enough to route the next
    # spreadsheet to WordPress. The user should not need a second button press.
    if value == '📦 مدیریت ارسال‌ها':
        ops.set_state(uid, 'site_tracking', 'waiting_file', {})
        return await update.message.reply_text(
            '📮 رهگیری پستی سایت\n\n'
            'فایل XLSX/XLSM/CSV را همینجا بفرستید. این بخش فقط جدول رهگیری سایت/ووکامرس را به‌روزرسانی می‌کند و هیچ کاری با سفارش‌های شاپینو ندارد.',
            reply_markup=st.site_tracking_menu(),
        )

    if value == '📤 آپلود اکسل کد رهگیری':
        ops.set_state(uid, 'site_tracking', 'waiting_file', {})
        return await update.message.reply_text(
            '📤 فایل رهگیری سایت را بفرستید.\n\n'
            'فرمت‌های قابل قبول: XLSX / XLSM / CSV\n'
            'این فایل فقط وارد افزونه رهگیری سایت می‌شود؛ مسیر شاپینو کاملاً جداست.',
            reply_markup=ops.cancel_menu(),
        )

    if value == '📤 ارسال فایل رهگیری':
        # Shopino spreadsheets are accepted only after this explicit action.
        ops.set_state(uid, 'shopino_tracking', 'waiting_file', {})
        return await update.message.reply_text(
            '📦 فایل رهگیری شاپینو (XLSX/XLSM) را همینجا ارسال کنید.',
            reply_markup=ops.shopino_menu(),
        )

    return await ORIG_TEXT(update, ctx)


async def document(update, ctx):
    if not await m.access(update):
        return

    uid = update.effective_user.id
    state = ops.get_state(uid)
    doc = update.message.document
    filename = (doc.file_name or '').strip()
    suffix = Path(filename).suffix.lower()

    # Website tracking has strict priority and can never fall through into the
    # legacy Shopino document handler.
    if state and state.get('flow') == 'site_tracking':
        return await st.document(update, ctx)

    # Shopino import is opt-in. Only the dedicated Shopino upload button creates
    # this state, preventing accidental cross-imports.
    if state and state.get('flow') == 'shopino_tracking':
        return await st.ORIG_DOCUMENT(update, ctx)

    # Never guess which system owns a spreadsheet. Previously the fallback was
    # Shopino, which caused website files to start scanning Shopino orders.
    if suffix in {'.xlsx', '.xlsm', '.csv', '.xls'}:
        return await update.message.reply_text(
            'این فایل هنوز به هیچ مسیر رهگیری وصل نشده است.\n\n'
            'برای سایت: «📦 مدیریت ارسال‌ها»\n'
            'برای شاپینو: «📦 رهگیری شاپینو» → «📤 ارسال فایل رهگیری»',
            reply_markup=st.main_menu(),
        )

    return await ORIG_DOCUMENT(update, ctx)


# ---------------------------------------------------------------------------
# Bridge fallback: relay timeout must not be the only route tried.
# ---------------------------------------------------------------------------
def _resilient_bridge_endpoints(self):
    relay = (os.getenv('BRIDGE_RELAY_URL') or '').rstrip('/')
    endpoints = []
    if relay and relay != self.url:
        endpoints.append(('cloudflare-relay', f'{relay}/'))
    endpoints.extend([
        ('admin-ajax', f'{self.url}/wp-admin/admin-ajax.php'),
        ('home', f'{self.url}/'),
        ('index', f'{self.url}/index.php'),
    ])
    return tuple(endpoints)


def _signed_get_resilient(self, op, payload=None):
    errors = []
    deadline = time.monotonic() + 16.0
    for label, endpoint in _resilient_bridge_endpoints(self):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        params = self._signed_params(op, payload)
        # One bad relay/route gets only a small slice of the budget so a direct
        # WordPress fallback always has a chance to run.
        attempt_timeout = min(4.5, remaining)
        try:
            response = self._stdlib_get(endpoint, params, attempt_timeout)
            return self._decode(response)
        except bridge.TRANSIENT_ERRORS as exc:
            errors.append(f'{label}: {exc}')
        except RuntimeError:
            raise
        except Exception as exc:
            errors.append(f'{label}: {exc}')
    raise RuntimeError('Signed GET Bridge ناموفق بود: ' + ' | '.join(errors[-4:]))


async def _async_signed_get_resilient(self, op, payload=None):
    errors = []
    deadline = time.monotonic() + 16.0
    for label, endpoint in _resilient_bridge_endpoints(self):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        params = self._signed_params(op, payload)
        attempt_timeout = min(4.5, remaining)
        try:
            response = await asyncio.wait_for(
                asyncio.to_thread(self._stdlib_get, endpoint, params, attempt_timeout),
                timeout=attempt_timeout + 0.25,
            )
            return self._decode(response)
        except bridge.TRANSIENT_ERRORS as exc:
            errors.append(f'{label}: {exc}')
        except RuntimeError:
            raise
        except Exception as exc:
            errors.append(f'{label}: {exc}')
    raise RuntimeError('Signed GET Bridge ناموفق بود: ' + ' | '.join(errors[-4:]))


bridge.BridgeWooClient._bridge_endpoints = _resilient_bridge_endpoints
bridge.BridgeWooClient._signed_get = _signed_get_resilient
bridge.BridgeWooClient._async_signed_get = _async_signed_get_resilient
