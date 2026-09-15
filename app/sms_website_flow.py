"""Website-order based tracking SMS flow.

The SMS feature is intentionally isolated from the marketplace tracking workflow.
Tracking spreadsheets are matched against WooCommerce orders from the Vesta website,
then an authorized admin explicitly confirms sending through Melipayamak.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import tempfile
import time
import uuid
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from app import main as m
from app import operations as ops

log = logging.getLogger('ordersvesta.sms.website')
IRAN_TZ = ZoneInfo('Asia/Tehran')
UTC = ZoneInfo('UTC')
STATE_PREFIX = 'sms_ui_'
MODE_PREFIX = 'sms_mode_'
MAX_ORDER_PAGES = 10
ORDERS_PER_PAGE = 100


def sms_menu():
    return ReplyKeyboardMarkup([
        ['📤 ارسال فایل رهگیری برای پیامک'],
        ['🧪 تست ارسال پیامک', '📊 گزارش پیامک‌ها'],
        ['⬅️ منوی اصلی'],
    ], resize_keyboard=True)


def _state_key(uid):
    return f'{STATE_PREFIX}{int(uid)}'


def _mode_key(uid):
    return f'{MODE_PREFIX}{int(uid)}'


def set_state(uid, step='', **data):
    payload = {'step': step, **data} if step else {}
    m.setv(_state_key(uid), json.dumps(payload, ensure_ascii=False))


def get_state(uid):
    raw = m.get(_state_key(uid)) or ''
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def clear_state(uid):
    m.setv(_state_key(uid), '')
    m.setv(_mode_key(uid), '0')


def waiting_for_document(uid):
    return m.get(_mode_key(uid)) in {'1', 'website', 'await_file'} or get_state(uid).get('step') == 'await_file'


def _norm(value, compact=False):
    return m.norm(str(value or ''), compact)


def _phone(value):
    phone = m.normalize_phone(value)
    return phone if re.fullmatch(r'09\d{9}', phone or '') else ''


def _order_record(order):
    billing = order.get('billing') or {}
    shipping = order.get('shipping') or {}
    first = shipping.get('first_name') or billing.get('first_name') or ''
    last = shipping.get('last_name') or billing.get('last_name') or ''
    city = shipping.get('city') or billing.get('city') or ''
    address = ' '.join(str(x or '').strip() for x in (
        shipping.get('address_1') or billing.get('address_1'),
        shipping.get('address_2') or billing.get('address_2'),
    ) if str(x or '').strip())
    return {
        'id': int(order.get('id') or 0),
        'number': str(order.get('number') or order.get('id') or ''),
        'status': str(order.get('status') or ''),
        'name': f'{first} {last}'.strip(),
        'city': str(city or '').strip(),
        'address': address,
        'phone': _phone(billing.get('phone') or shipping.get('phone') or order.get('billing_phone')),
        'date_created': str(order.get('date_created') or ''),
    }


def _pct(value):
    return quote(str(value), safe='~-._')


def _oauth_params(ck, cs, method, url, params):
    signed = dict(params or {})
    signed.update({
        'oauth_consumer_key': ck,
        'oauth_nonce': secrets.token_hex(16),
        'oauth_signature_method': 'HMAC-SHA256',
        'oauth_timestamp': str(int(time.time())),
    })
    pairs = []
    for key, value in signed.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            pairs.append((_pct(key), _pct(item)))
    pairs.sort(key=lambda item: (item[0], item[1]))
    normalized = '&'.join(f'{k}={v}' for k, v in pairs)
    base_string = '&'.join((_pct(method.upper()), _pct(url), _pct(normalized)))
    signing_key = f'{_pct(cs)}&'.encode()
    digest = hmac.new(signing_key, base_string.encode(), hashlib.sha256).digest()
    signed['oauth_signature'] = base64.b64encode(digest).decode()
    return signed


def _request_woo_page(site_url, page):
    """Read one WooCommerce order page without using any marketplace session."""
    url = f'{site_url.rstrip("/")}/wp-json/wc/v3/orders'
    params = {
        'page': int(page),
        'per_page': ORDERS_PER_PAGE,
        'orderby': 'date',
        'order': 'desc',
    }
    ck = (ops.cfg_get('ck') or '').strip()
    cs = (ops.cfg_get('cs') or '').strip()
    wp_user = (ops.cfg_get('wp_user') or '').strip()
    wp_pass = (ops.cfg_get('wp_app_password') or '').strip()
    timeout = httpx.Timeout(connect=20.0, read=60.0, write=30.0, pool=10.0)
    attempts = []

    with httpx.Client(timeout=timeout, follow_redirects=True, trust_env=False) as client:
        if wp_user and wp_pass:
            passwords = [wp_pass]
            compact = wp_pass.replace(' ', '')
            if compact != wp_pass:
                passwords.append(compact)
            for password in passwords:
                try:
                    response = client.get(url, auth=(wp_user, password), params=params)
                    if response.status_code < 400:
                        return response
                    attempts.append(f'WP-App HTTP {response.status_code}')
                except Exception as exc:
                    attempts.append(f'WP-App {type(exc).__name__}')

        if ck and cs:
            try:
                response = client.get(url, auth=(ck, cs), params=params)
                if response.status_code < 400:
                    return response
                attempts.append(f'Woo-Basic HTTP {response.status_code}')
            except Exception as exc:
                attempts.append(f'Woo-Basic {type(exc).__name__}')

            try:
                oauth = _oauth_params(ck, cs, 'GET', url, params)
                response = client.get(url, params=oauth)
                if response.status_code < 400:
                    return response
                attempts.append(f'Woo-OAuth HTTP {response.status_code}')
            except Exception as exc:
                attempts.append(f'Woo-OAuth {type(exc).__name__}')

    raise RuntimeError('دریافت سفارش‌های سایت از REST ناموفق بود: ' + ' | '.join(attempts[-4:]))


def _fetch_orders_via_bridge(page):
    client = ops.WooClient()
    try:
        result = client._signed_get('sms_orders', {
            'page': int(page),
            'per_page': ORDERS_PER_PAGE,
        })
        return result
    finally:
        try:
            close = getattr(client, 'close', None)
            if close:
                close()
        except Exception:
            pass


def fetch_website_orders():
    site_url = (ops.cfg_get('url') or '').rstrip('/')
    if not site_url:
        raise RuntimeError('اتصال سایت تنظیم نشده است. از بخش «اتصال ووکامرس» استفاده کنید.')

    direct_available = any((
        (ops.cfg_get('ck') or '').strip() and (ops.cfg_get('cs') or '').strip(),
        (ops.cfg_get('wp_user') or '').strip() and (ops.cfg_get('wp_app_password') or '').strip(),
    ))
    rows = []
    direct_error = None
    bridge_error = None

    if direct_available:
        try:
            for page in range(1, MAX_ORDER_PAGES + 1):
                response = _request_woo_page(site_url, page)
                batch = response.json()
                if not isinstance(batch, list):
                    raise RuntimeError('پاسخ سفارش‌های سایت معتبر نیست.')
                rows.extend(batch)
                total_pages = int(response.headers.get('x-wp-totalpages') or 1)
                if page >= total_pages or len(batch) < ORDERS_PER_PAGE:
                    break
        except Exception as exc:
            direct_error = exc
            rows = []

    if not rows:
        try:
            for page in range(1, MAX_ORDER_PAGES + 1):
                result = _fetch_orders_via_bridge(page)
                batch = result.get('orders') or []
                rows.extend(batch)
                total_pages = int(result.get('total_pages') or 1)
                if page >= total_pages or len(batch) < ORDERS_PER_PAGE:
                    break
        except Exception as exc:
            bridge_error = exc

    if not rows:
        details = []
        if direct_error:
            details.append(str(direct_error))
        if bridge_error:
            details.append(str(bridge_error))
        raise RuntimeError(
            'دریافت سفارش‌های سایت ممکن نشد. ' +
            (' | '.join(details[-2:]) if details else 'اتصال WooCommerce را بررسی کنید.')
        )

    records = []
    seen = set()
    for raw in rows:
        # Bridge returns an already-normalized compact record; REST returns WC JSON.
        record = raw if {'name', 'city', 'phone'}.issubset(raw.keys()) else _order_record(raw)
        try:
            oid = int(record.get('id') or 0)
        except Exception:
            oid = 0
        if not oid or oid in seen:
            continue
        seen.add(oid)
        status = str(record.get('status') or '').lower()
        if status in {'cancelled', 'refunded', 'failed', 'trash'}:
            continue
        record = dict(record)
        record['id'] = oid
        record['phone'] = _phone(record.get('phone'))
        records.append(record)
    return records


def _city_similarity(a, b):
    a = _norm(a, True)
    b = _norm(b, True)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if a in b or b in a:
        return 0.94
    return SequenceMatcher(None, a, b).ratio()


def _name_similarity(a, b):
    a = _norm(a, True)
    b = _norm(b, True)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def candidates(row, orders):
    result = []
    for order in orders:
        ns = _name_similarity(row.get('name'), order.get('name'))
        cs = _city_similarity(row.get('city'), order.get('city'))
        if ns < 0.72:
            continue
        # The postal file normally includes city. When it does, require a meaningful city match.
        if _norm(row.get('city'), True) and cs < 0.58:
            continue
        score = (ns * 78.0) + (cs * 22.0)
        if _norm(row.get('address'), True) and _norm(order.get('address'), True):
            address_ratio = SequenceMatcher(
                None,
                _norm(row.get('address'), True),
                _norm(order.get('address'), True),
            ).ratio()
            score += min(5.0, address_ratio * 5.0)
        result.append({
            'order': order,
            'name_score': ns,
            'city_score': cs,
            'score': round(score, 2),
        })
    result.sort(key=lambda item: (-item['score'], -item['order']['id']))
    return result[:6]


def confident(matches):
    if not matches:
        return False
    top = matches[0]
    if top['name_score'] < 0.80 or top['city_score'] < 0.68:
        return False
    if len(matches) == 1:
        return True
    return top['score'] - matches[1]['score'] >= 7.0


def _mask_phone(phone):
    phone = str(phone or '')
    return f'••••{phone[-4:]}' if len(phone) >= 4 else '—'


def _preview_text(items, unmatched, total_rows):
    price = float(__import__('os').getenv('MELLIPAYAMAK_PRICE_PER_SEGMENT', '0') or 0)
    segments = sum(m.sms_segments(m.SMS_TEXT.format(code=x['code'])) for x in items)
    cost = segments * price
    lines = [
        '📊 پیش‌نمایش ارسال پیامک',
        '',
        f'ردیف‌های فایل: {total_rows}',
        f'✅ تطبیق قطعی با سفارش سایت: {len(items)}',
        f'⚠️ بدون تطبیق قطعی/بدون موبایل: {len(unmatched)}',
        f'💬 تعداد بخش پیامک: {segments}',
    ]
    if price > 0:
        lines.append(f'💳 هزینه تقریبی: {cost:,.0f} تومان')
    if items:
        lines.extend(['', 'نمونه تطبیق‌ها:'])
        for item in items[:5]:
            lines.append(
                f'• سفارش #{item["order_number"]} — {item["name"]} | {item["city"]} | {_mask_phone(item["phone"])}'
            )
        if len(items) > 5:
            lines.append(f'… و {len(items) - 5} مورد دیگر')
    lines.extend(['', 'ارسال فقط بعد از تأیید مدیر انجام می‌شود.'])
    return '\n'.join(lines), segments, cost


async def sms_command(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    set_state(uid, 'await_file')
    m.setv(_mode_key(uid), 'await_file')
    await update.effective_chat.send_message(
        '📱 پیامک کد رهگیری\n\n'
        'فایل خروجی پست (xlsx/xlsm) را بفرستید. ربات هر ردیف را با سفارش‌های سایت Vesta '
        'بر اساس نام و شهر تطبیق می‌دهد، شماره موبایل را از همان سفارش سایت برمی‌دارد و قبل از ارسال پیش‌نمایش نشان می‌دهد.',
        reply_markup=sms_menu(),
    )


async def document(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    d = update.message.document
    suffix = Path(d.file_name or '').suffix.lower()
    if suffix not in {'.xlsx', '.xlsm'}:
        return await update.message.reply_text('فقط فایل xlsx/xlsm ارسال کنید.', reply_markup=sms_menu())

    # Consume document mode now; retry is explicitly offered if anything fails.
    m.setv(_mode_key(uid), '0')
    set_state(uid, 'processing')
    progress = await update.message.reply_text('📥 فایل دریافت شد. در حال خواندن سفارش‌های سایت و تطبیق نام/شهر…')
    tmp = Path(tempfile.gettempdir()) / f'sms_{uuid.uuid4().hex}{suffix}'
    try:
        tg_file = await d.get_file(read_timeout=60, connect_timeout=30, pool_timeout=30)
        await tg_file.download_to_drive(
            custom_path=str(tmp), read_timeout=90, connect_timeout=30, pool_timeout=30
        )
        rows = await asyncio.to_thread(m.parse_excel, tmp)
        await progress.edit_text(f'📄 {len(rows)} کد رهگیری پیدا شد. در حال دریافت سفارش‌های سایت…')
        orders = await asyncio.to_thread(fetch_website_orders)
        await progress.edit_text(f'🔎 {len(orders)} سفارش سایت بررسی شد. در حال تطبیق نهایی…')

        items = []
        unmatched = []
        for row in rows:
            matches = candidates(row, orders)
            if not confident(matches):
                unmatched.append({'row': row, 'reason': 'تطبیق قطعی پیدا نشد'})
                continue
            order = matches[0]['order']
            phone = _phone(order.get('phone'))
            if not phone:
                unmatched.append({'row': row, 'reason': 'شماره موبایل در سفارش سایت ثبت نشده'})
                continue
            items.append({
                'row': int(row.get('row') or 0),
                'website_order_id': int(order['id']),
                'order_number': str(order.get('number') or order['id']),
                'name': str(order.get('name') or row.get('name') or ''),
                'city': str(order.get('city') or row.get('city') or ''),
                'phone': phone,
                'code': str(row['code']),
            })

        text, segments, cost = _preview_text(items, unmatched, len(rows))
        payload = {
            'kind': 'website_tracking_sms',
            'items': items,
            'unmatched': unmatched,
            'segments': segments,
            'cost': cost,
            'source': 'woocommerce',
        }
        token = m.save_pending(update.effective_chat.id, uid, payload)
        set_state(uid, 'preview', token=token)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton('✅ تأیید و ارسال', callback_data=f'sms:send:{token}'),
                InlineKeyboardButton('⏰ زمان‌بندی', callback_data=f'sms:schedule:{token}'),
            ],
            [InlineKeyboardButton('❌ لغو', callback_data=f'sms:cancel:{token}')],
        ])
        await progress.edit_text(text, reply_markup=kb)
    except Exception as exc:
        log.exception('website SMS spreadsheet processing failed')
        set_state(uid, 'await_file')
        m.setv(_mode_key(uid), 'await_file')
        await progress.edit_text(
            f'❌ آماده‌سازی پیامک انجام نشد:\n{exc}\n\nفایل را دوباره بفرستید.'
        )
    finally:
        tmp.unlink(missing_ok=True)


async def _send_payload(token, payload):
    sent = 0
    failed = 0
    failures = []
    for item in payload.get('items') or []:
        try:
            await asyncio.to_thread(m.melipayamak_send, item['phone'], m.SMS_TEXT.format(code=item['code']))
            sent += 1
            with m.db() as conn:
                conn.execute(
                    'INSERT INTO sms_log(token,phone,status,detail) VALUES(?,?,?,?)',
                    (token, item['phone'], 'sent', f'website_order:{item.get("website_order_id", "")}'),
                )
        except Exception as exc:
            failed += 1
            failures.append(str(exc))
            with m.db() as conn:
                conn.execute(
                    'INSERT INTO sms_log(token,phone,status,detail) VALUES(?,?,?,?)',
                    (token, item.get('phone', ''), 'failed', str(exc)[:300]),
                )
    return sent, failed, failures


async def callback(update, ctx, fallback):
    q = update.callback_query
    data = q.data or ''
    if data != 'menu_sms' and not data.startswith('sms:'):
        return await fallback(update, ctx)
    if not m.allowed(q.from_user.id):
        return await q.answer('دسترسی ندارید', show_alert=True)
    await q.answer()
    uid = q.from_user.id

    if data == 'menu_sms':
        set_state(uid, 'await_file')
        m.setv(_mode_key(uid), 'await_file')
        return await q.edit_message_text(
            '📱 پیامک کد رهگیری\n\nفایل خروجی پست (xlsx/xlsm) را بفرستید. '
            'تطبیق با سفارش‌های سایت Vesta انجام می‌شود و قبل از ارسال، پیش‌نمایش و تأیید نمایش داده می‌شود.'
        )

    parts = data.split(':')
    action = parts[1] if len(parts) > 1 else ''

    if action == 'test':
        set_state(uid, 'test_phone')
        return await q.edit_message_text('🧪 تست پیامک\nشماره موبایل مقصد را بفرستید؛ مثال: 09123456789')

    if action == 'report':
        with m.db() as conn:
            rows = conn.execute(
                'SELECT status,COUNT(*) AS n FROM sms_log GROUP BY status'
            ).fetchall()
            recent = conn.execute(
                'SELECT phone,status,created_at FROM sms_log ORDER BY id DESC LIMIT 8'
            ).fetchall()
        counts = {r['status']: int(r['n']) for r in rows}
        lines = [
            '📊 گزارش پیامک‌ها',
            f'✅ موفق: {counts.get("sent", 0)}',
            f'❌ ناموفق: {counts.get("failed", 0)}',
        ]
        if recent:
            lines.extend(['', 'آخرین ارسال‌ها:'])
            for r in recent:
                lines.append(f'• {_mask_phone(r["phone"])} — {r["status"]} — {r["created_at"]}')
        return await q.edit_message_text('\n'.join(lines))

    if action == 'test_send':
        state = get_state(uid)
        if state.get('step') != 'test_confirm':
            return await q.edit_message_text('این تست منقضی شده؛ دوباره تست پیامک را شروع کنید.')
        try:
            await asyncio.to_thread(
                m.melipayamak_send,
                state['phone'],
                m.SMS_TEXT.format(code=state['code']),
            )
            clear_state(uid)
            return await q.edit_message_text('✅ پیامک تست با موفقیت ارسال شد.')
        except Exception as exc:
            return await q.edit_message_text(f'❌ ارسال تست ناموفق بود: {exc}')

    if action == 'test_cancel':
        clear_state(uid)
        return await q.edit_message_text('❌ تست لغو شد.')

    if len(parts) < 3:
        return await q.edit_message_text('این عملیات معتبر نیست.')
    token = parts[2]
    pending = m.pending(token)
    if not pending or pending['status'] != 'pending':
        return await q.edit_message_text('این ارسال قبلاً بررسی شده یا منقضی شده است.')
    payload = pending['payload']
    if payload.get('kind') != 'website_tracking_sms':
        return await q.edit_message_text('این ارسال متعلق به بخش پیامک سایت نیست.')

    if action == 'cancel':
        m.done(token, 'sms_cancelled')
        clear_state(uid)
        return await q.edit_message_text('❌ ارسال لغو شد؛ هیچ پیامکی ارسال نشد.')

    if action == 'schedule':
        set_state(uid, 'schedule_time', token=token)
        return await q.edit_message_text(
            '⏰ زمان ارسال را به وقت ایران بفرستید.\nمثال: 2026-09-16 18:30'
        )

    if action == 'send':
        if not payload.get('items'):
            return await q.edit_message_text('❌ مورد قابل ارسالی وجود ندارد.')
        await q.edit_message_text(f'📤 در حال ارسال {len(payload["items"])} پیامک…')
        sent, failed, failures = await _send_payload(token, payload)
        m.done(token, 'sms_sent' if failed == 0 else 'sms_partial')
        clear_state(uid)
        text = f'✅ ارسال تمام شد.\nموفق: {sent}\nناموفق: {failed}'
        if failures:
            text += f'\n\nآخرین خطا: {failures[-1][:220]}'
        return await q.edit_message_text(text)

    return await q.edit_message_text('عملیات ناشناخته است.')


async def text(update, ctx, fallback):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    value = (update.message.text or '').strip()
    state = get_state(uid)

    if value in {'📮 ارسال پیامک رهگیری', '📮 ارسال پیامک کد رهگیری', 'مدیریت ارسال‌ها', '📤 ارسال فایل رهگیری برای پیامک'}:
        return await sms_command(update, ctx)
    if value in {'🧪 تست ارسال پیامک', '🧪 تست پیامک'}:
        set_state(uid, 'test_phone')
        m.setv(_mode_key(uid), '0')
        return await update.message.reply_text(
            '🧪 تست پیامک\nشماره موبایل مقصد را بفرستید؛ مثال: 09123456789',
            reply_markup=sms_menu(),
        )
    if value == '📊 گزارش پیامک‌ها':
        with m.db() as conn:
            rows = conn.execute('SELECT status,COUNT(*) AS n FROM sms_log GROUP BY status').fetchall()
        counts = {r['status']: int(r['n']) for r in rows}
        return await update.message.reply_text(
            f'📊 گزارش پیامک‌ها\n✅ موفق: {counts.get("sent", 0)}\n❌ ناموفق: {counts.get("failed", 0)}',
            reply_markup=sms_menu(),
        )
    if value == '⬅️ منوی اصلی' and state:
        clear_state(uid)
        return await fallback(update, ctx)

    step = state.get('step')
    if step == 'test_phone':
        phone = _phone(value)
        if not phone:
            return await update.message.reply_text('شماره معتبر نیست؛ مثال: 09123456789')
        set_state(uid, 'test_code', phone=phone)
        return await update.message.reply_text('کد رهگیری تستی را بفرستید.')

    if step == 'test_code':
        code = re.sub(r'\s+', '', value.translate(m.DIGITS))
        if not code:
            return await update.message.reply_text('کد رهگیری نمی‌تواند خالی باشد.')
        set_state(uid, 'test_confirm', phone=state['phone'], code=code)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ ارسال تست', callback_data='sms:test_send'),
            InlineKeyboardButton('❌ لغو', callback_data='sms:test_cancel'),
        ]])
        return await update.message.reply_text(
            f'📱 شماره: {_mask_phone(state["phone"])}\n📦 کد رهگیری: {code}\n\nارسال شود؟',
            reply_markup=kb,
        )

    if step == 'schedule_time':
        token = state.get('token')
        pending = m.pending(token) if token else None
        if not pending or pending['status'] != 'pending':
            clear_state(uid)
            return await update.message.reply_text('این ارسال منقضی شده است.')
        try:
            dt = datetime.strptime(value, '%Y-%m-%d %H:%M').replace(tzinfo=IRAN_TZ)
            if dt <= datetime.now(IRAN_TZ):
                raise ValueError('past')
            with m.db() as conn:
                conn.execute(
                    'INSERT INTO sms_schedule(token,run_at,status) VALUES(?,?,?)',
                    (token, dt.astimezone(UTC).isoformat(), 'pending'),
                )
            clear_state(uid)
            return await update.message.reply_text(
                '✅ ارسال زمان‌بندی شد: ' + dt.strftime('%Y/%m/%d %H:%M') + ' به وقت ایران',
                reply_markup=sms_menu(),
            )
        except Exception:
            return await update.message.reply_text('فرمت زمان درست نیست. مثال: 2026-09-16 18:30')

    return await fallback(update, ctx)


async def scheduler():
    while True:
        try:
            now = datetime.now(UTC).isoformat()
            with m.db() as conn:
                jobs = conn.execute(
                    "SELECT id,token FROM sms_schedule WHERE status='pending' AND run_at<=? ORDER BY id LIMIT 10",
                    (now,),
                ).fetchall()
            for job in jobs:
                with m.db() as conn:
                    conn.execute("UPDATE sms_schedule SET status='running' WHERE id=?", (job['id'],))
                pending = m.pending(job['token'])
                if not pending or pending['status'] != 'pending':
                    with m.db() as conn:
                        conn.execute("UPDATE sms_schedule SET status='cancelled' WHERE id=?", (job['id'],))
                    continue
                payload = pending['payload']
                if payload.get('kind') != 'website_tracking_sms':
                    with m.db() as conn:
                        conn.execute("UPDATE sms_schedule SET status='cancelled' WHERE id=?", (job['id'],))
                    continue
                sent, failed, _ = await _send_payload(job['token'], payload)
                with m.db() as conn:
                    conn.execute(
                        "UPDATE sms_schedule SET status=? WHERE id=?",
                        ('sent' if failed == 0 else 'partial', job['id']),
                    )
                m.done(job['token'], 'sms_scheduled_sent' if failed == 0 else 'sms_scheduled_partial')
                log.info('scheduled website SMS token=%s sent=%s failed=%s', job['token'], sent, failed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception('website SMS scheduler failed')
        await asyncio.sleep(20)


_SCHEDULER_TASK = None


async def startup(application):
    global _SCHEDULER_TASK
    log.info(
        'website SMS source config: woo_keys=%s wp_app=%s bridge_token=%s',
        bool((ops.cfg_get('ck') or '') and (ops.cfg_get('cs') or '')),
        bool((ops.cfg_get('wp_user') or '') and (ops.cfg_get('wp_app_password') or '')),
        bool(ops.cfg_get('bridge_token') or ''),
    )
    if _SCHEDULER_TASK is None or _SCHEDULER_TASK.done():
        _SCHEDULER_TASK = asyncio.create_task(scheduler())
