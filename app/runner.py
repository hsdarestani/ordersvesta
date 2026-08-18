import asyncio
import logging
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import main as m

log = logging.getLogger('ordersvesta.runner')


# Shopino's order payloads are large (products/media/submission logs). The panel itself
# requests only 10 rows at a time. Bulk-loading 100 rows can therefore hit read timeouts.
# Use smaller pages, longer read timeouts and retry transient network failures.
def resilient_init(self, sid):
    self.h = dict(m.WEB_HEADERS)
    self.h['cookie'] = f'sessionid={sid}'
    timeout = httpx.Timeout(connect=20.0, read=90.0, write=30.0, pool=30.0)
    self.c = httpx.Client(timeout=timeout, follow_redirects=True, headers=self.h)


m.Shopino.__init__ = resilient_init


def request_with_retry(client, method, url, **kwargs):
    last = None
    for attempt in range(1, 4):
        try:
            return client.request(method, url, **kwargs)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ConnectError) as exc:
            last = exc
            log.warning('Shopino transient network error attempt=%s: %s', attempt, exc)
            if attempt < 3:
                time.sleep(attempt * 1.5)
    raise RuntimeError(f'ارتباط با شاپینو بعد از ۳ تلاش ناموفق بود: {last}')


def fetch_orders_for_rows(client, rows):
    """Fetch newest orders progressively and stop once every Excel row has a candidate.

    We intentionally fetch a few extra pages after all rows are found so duplicate/similar
    names near the same shipment batch still surface as ambiguous instead of auto-matching.
    If rows remain unresolved, pagination continues through the full Shopino history.
    """
    url = f'{m.BASE}/shops/{m.SHOP}/order-shippings/'
    params = {'page': 1, 'page_size': 20}
    out = []
    seen = set()
    page = 0
    extra_pages_after_complete = None

    while url:
        page += 1
        r = request_with_retry(client.c, 'GET', url, params=params)
        client.check(r)
        data = r.json()
        params = None

        for item in data.get('results', []):
            oid = item.get('id')
            if oid not in seen:
                seen.add(oid)
                out.append(item)

        url = data.get('next')

        # Don't evaluate too early; a current shipping batch can span several pages.
        if page >= 5:
            all_have_candidate = all(bool(m.candidates(row, out)) for row in rows)
            if all_have_candidate and extra_pages_after_complete is None:
                extra_pages_after_complete = 5
            elif extra_pages_after_complete is not None:
                extra_pages_after_complete -= 1
                if extra_pages_after_complete <= 0:
                    break

        if len(out) > 5000:
            break

    return out


async def download_telegram_file(document, target):
    """Telegram's default HTTPX read timeout is short; retry with explicit long timeouts."""
    last = None
    for attempt in range(1, 4):
        try:
            f = await document.get_file(
                read_timeout=45,
                write_timeout=45,
                connect_timeout=30,
                pool_timeout=30,
            )
            await f.download_to_drive(
                custom_path=str(target),
                read_timeout=90,
                write_timeout=45,
                connect_timeout=30,
                pool_timeout=30,
            )
            return
        except Exception as exc:
            last = exc
            log.warning('Telegram file download failed attempt=%s: %r', attempt, exc)
            if attempt < 3:
                await asyncio.sleep(attempt * 2)
    raise RuntimeError(f'دانلود فایل از تلگرام بعد از ۳ تلاش ناموفق بود: {last}')


async def document(update, ctx):
    if not await m.access(update):
        return
    try:
        client = m.api()
    except Exception as exc:
        return await update.message.reply_text(f'⚠️ {exc}\n/login')

    d = update.message.document
    suffix = Path(d.file_name or '').suffix.lower()
    if suffix not in {'.xlsx', '.xlsm'}:
        return await update.message.reply_text('فقط xlsx/xlsm بفرستید.')

    msg = await update.message.reply_text('📥 در حال دانلود و خواندن فایل…')
    tmp = Path(tempfile.gettempdir()) / f'{uuid.uuid4().hex}{suffix}'

    try:
        await download_telegram_file(d, tmp)
        rows = await asyncio.to_thread(m.parse_excel, tmp)
        await msg.edit_text(
            f'📄 {len(rows)} بارکد پیدا شد.\n'
            '🔎 در حال دریافت سفارش‌های مرتبط شاپینو… لطفاً کمی صبر کنید.'
        )

        orders = await asyncio.to_thread(fetch_orders_for_rows, client, rows)
        await msg.edit_text(
            f'📄 {len(rows)} بارکد | {len(orders)} سفارش شاپینو بررسی شد.\n'
            '⚙️ در حال تطبیق و ثبت کدها…'
        )

        ok = old = review = fail = ignored = 0
        pend = []
        for i, row in enumerate(rows, 1):
            cs = m.candidates(row, orders)

            # No plausible Shopino match = ignore silently. These shipments may have
            # originated from the website or another sales channel and must never be
            # shown as a manual/skip question to the admin.
            if not cs:
                ignored += 1
                continue

            if m.confident(cs):
                candidate = cs[0]
                if candidate['tracking'] == row['code']:
                    old += 1
                elif candidate['tracking']:
                    review += 1
                    pend.append({'row': row, 'candidates': cs})
                else:
                    try:
                        await asyncio.to_thread(
                            request_patch_with_retry,
                            client,
                            candidate['id'],
                            row['code'],
                            candidate['type'],
                        )
                        ok += 1
                        candidate['tracking'] = row['code']
                    except m.AuthError:
                        raise
                    except Exception as exc:
                        log.exception('Tracking PATCH failed order=%s', candidate['id'])
                        fail += 1
            else:
                review += 1
                pend.append({'row': row, 'candidates': cs})

            if i % 10 == 0:
                try:
                    await msg.edit_text(
                        f'⏳ {i}/{len(rows)} | ✅ ثبت {ok} | ♻️ قبلی {old} | '
                        f'⚠️ بررسی {review} | 🚫 بدون تطبیق {ignored} | ❌ {fail}'
                    )
                except Exception:
                    pass

        await msg.edit_text(
            f'✅ پردازش تمام شد\n\n'
            f'ثبت خودکار: {ok}\n'
            f'از قبل ثبت: {old}\n'
            f'نیاز به تأیید: {review}\n'
            f'بدون مشابه در شاپینو: {ignored}\n'
            f'خطا: {fail}'
        )

        for payload in pend:
            token = m.save_pending(update.effective_chat.id, update.effective_user.id, payload)
            await m.ask(update.effective_chat, token, payload)

    except m.AuthError:
        await msg.edit_text('🔐 نشست شاپینو منقضی شده. /login را بزنید و دوباره فایل را بفرستید.')
    except Exception as exc:
        log.exception('import failed')
        await msg.edit_text(f'❌ خطا: {exc}')
    finally:
        tmp.unlink(missing_ok=True)


def request_patch_with_retry(client, order_id, code, typ='post'):
    r = request_with_retry(
        client.c,
        'PATCH',
        f'{m.BASE}/shops/{m.SHOP}/order-shippings/{order_id}/',
        json={'tracking_code': str(code), 'type': typ or 'post'},
    )
    client.check(r)
    return True


# ---------- Permissions / Shopino login ----------
# The Shopino session is shared by the bot because all admins operate the same Vesta
# Shopino account. Any admin explicitly added with /allow can therefore authenticate.
async def admin_login_cmd(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    m.clear_login_state(uid)
    m.set_login_state(uid, 'phone')
    await update.message.reply_text(
        '🔐 ورود شاپینو\n\n'
        'شماره موبایل اکانت شاپینو را بفرستید.\n'
        'مثال: 09123456789\n\n'
        '/cancel برای لغو'
    )


async def admin_session(update, ctx):
    if not await m.access(update):
        return
    if not ctx.args:
        return await update.message.reply_text(
            'حالت پشتیبان: /session SESSION_ID\nبرای ورود عادی از /login استفاده کنید.'
        )
    text = ' '.join(ctx.args)
    import re
    match = re.search(r'sessionid=([^;\s]+)', text)
    sid = match.group(1) if match else text.strip().split()[0]
    try:
        count = await asyncio.to_thread(m.Shopino(sid).probe)
        m.setv('session', sid)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.effective_chat.send_message(f'✅ اتصال شاپینو برقرار شد؛ {count} سفارش قابل مشاهده است.')
    except Exception as exc:
        await update.message.reply_text(f'❌ سشن ذخیره نشد: {exc}')


async def admin_text(update, ctx):
    if not await m.access(update):
        return

    uid = update.effective_user.id
    text_value = (update.message.text or '').strip()
    state = m.login_state(uid)

    if state:
        if state['step'] == 'phone':
            phone = m.normalize_phone(text_value)
            try:
                phone = await asyncio.to_thread(m.send_verification_code, phone)
                m.set_login_state(uid, 'code', phone)
                try:
                    await update.message.delete()
                except Exception:
                    pass
                return await update.effective_chat.send_message(
                    f'📲 کد تأیید شاپینو به شماره ••••{phone[-4:]} ارسال شد.\n'
                    'کد را همینجا بفرستید.\n\n/cancel برای لغو'
                )
            except Exception as exc:
                return await update.message.reply_text(
                    f'❌ ارسال کد انجام نشد: {exc}\nشماره را دوباره بفرستید.'
                )

        if state['step'] == 'code':
            code = text_value.translate(m.DIGITS)
            try:
                sid, count = await asyncio.to_thread(m.verify_phone, state['phone'], code)
                m.setv('session', sid)
                m.clear_login_state(uid)
                try:
                    await update.message.delete()
                except Exception:
                    pass
                return await update.effective_chat.send_message(
                    f'✅ ورود شاپینو موفق بود.\n'
                    f'اتصال API برقرار است و {count} سفارش قابل مشاهده است.\n\n'
                    'حالا فایل اکسل را بفرستید.'
                )
            except Exception as exc:
                return await update.message.reply_text(
                    f'❌ ورود ناموفق: {exc}\n'
                    'کد را دوباره بفرستید یا /login را از اول بزنید.'
                )

    # Preserve the existing manual order-ID confirmation flow for admins.
    with m.db() as c:
        r = c.execute('SELECT token FROM manual WHERE user=?', (uid,)).fetchone()
    if not r:
        return
    t = r['token']
    s = text_value.translate(m.DIGITS)
    if not s.isdigit():
        return await update.message.reply_text('فقط ID عددی را بفرستید.')
    try:
        o = await asyncio.to_thread(m.api().one, int(s))
        name = m.full_name(o)
        city = str((o.get('order') or {}).get('city') or '')
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ تأیید و ثبت', callback_data=f'c:{t}:{s}'),
            InlineKeyboardButton('لغو', callback_data=f's:{t}'),
        ]])
        await update.message.reply_text(
            f'سفارش پیدا شد: {s} | {name} | {city}\nثبت روی همین سفارش؟',
            reply_markup=kb,
        )
    except m.AuthError:
        await update.message.reply_text('🔐 نشست شاپینو منقضی شده. /login را بزنید.')
    except Exception as exc:
        await update.message.reply_text(f'❌ {exc}')


# Replace the handlers before main() starts. Any admin added through /allow can now
# authenticate with OTP or use the fallback session command.
m.login_cmd = admin_login_cmd
m.session = admin_session
m.text = admin_text
m.document = document


if __name__ == '__main__':
    m.main()
