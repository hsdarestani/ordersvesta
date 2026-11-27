"""Admin-only stock receipt wizard using the existing signed Woo bridge."""
import asyncio
import base64
import io
import re
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from app import main as m, operations as ops

LABEL = '📦 شارژ موجودی'
FLOW = 'stock_receipt'
LOCKS = {}
TTL = 900


def quantity(value):
    value = str(value).translate(m.DIGITS).strip()
    if not re.fullmatch(r'[0-9]{1,5}', value):
        raise ValueError('تعداد را به صورت عدد صحیح مثبت وارد کنید.')
    n = int(value)
    if not 1 <= n <= 10000:
        raise ValueError('تعداد باید بین ۱ تا ۱۰۰۰۰ باشد.')
    return n


def keyboard(rows):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(title, callback_data=action) for title, action in row]
        for row in rows
    ])


def session(uid, step, data):
    data['expires'] = time.time() + TTL
    ops.set_state(uid, FLOW, step, data)


def live_state(uid, chat):
    state = ops.get_state(uid)
    if not state or state['flow'] != FLOW:
        return None
    d = state['data']
    if d.get('chat') != chat or d.get('expires', 0) < time.time():
        return None
    return state


async def call(op, data):
    return await asyncio.to_thread(lambda: ops.WooClient().call(op, data))


async def begin(update):
    uid = update.effective_user.id
    session(uid, 'search', {'chat': update.effective_chat.id})
    return await update.effective_message.reply_text(
        '📦 شارژ موجودی\nبخشی از نام محصول یا کد کالا را بفرستید.\n'
        'محصولات ناموجود نیز در نتایج نمایش داده می‌شوند.',
        reply_markup=ops.cancel_menu())


def describe(p):
    return (
        f"{p['name']}\nکد: {p.get('sku') or 'ندارد'} | شناسه: {p['id']}\n"
        f"مدل: {p.get('attributes') or 'بدون تنوع'}\n"
        f"موجودی فعلی: {p.get('quantity') if p.get('quantity') is not None else 'نامشخص'}"
    )[:850]


async def cards(chat, uid, d, response):
    items = response.get('products', [])
    d.update(items={str(p['id']): p for p in items}, token=secrets.token_hex(6))
    session(uid, 'choose', d)
    if not items:
        return await chat.send_message('محصولی پیدا نشد. نام یا کد دیگری بفرستید.')
    for p in items:
        k = keyboard([[('انتخاب مدل' if p['type'] == 'variable' else 'انتخاب این محصول',
                       f"stock:pick:{d['token']}:{p['id']}")]])
        caption = describe(p)
        try:
            photo = await call('stock_photo', {'id': p['id']})
            raw = base64.b64decode(photo.get('image', ''), validate=True)
            if not raw:
                raise ValueError('no image')
            image = io.BytesIO(raw)
            image.name = 'product.jpg'
            await chat.send_photo(photo=image, caption=caption, reply_markup=k)
        except Exception:
            await chat.send_message(caption + '\n⚠️ عکس در دسترس نیست؛ کد و مدل را بررسی کنید.', reply_markup=k)
    if response.get('more'):
        await chat.send_message('نتایج بیشتر:', reply_markup=keyboard([
            [('صفحه بعد', f"stock:next:{d['token']}")]]))


async def search(chat, uid, d, value):
    d.update(query=value, page=1, parent=0)
    await cards(chat, uid, d, await call('stock_search', {'q': value, 'page': 1}))


async def text(update, ctx):
    uid, chat = update.effective_user.id, update.effective_chat
    value = (update.effective_message.text or '').strip()
    if value == LABEL:
        if chat.type != 'private' or not m.allowed(uid):
            return await update.effective_message.reply_text('این بخش فقط برای ادمین‌ها در گفت‌وگوی خصوصی فعال است.')
        async with LOCKS.setdefault(uid, asyncio.Lock()):
            await begin(update)
        return True
    state = ops.get_state(uid)
    if not state or state['flow'] != FLOW:
        return False
    if chat.type != 'private' or not m.allowed(uid):
        return True
    if value in {'❌ لغو عملیات', '⬅️ منوی اصلی'}:
        async with LOCKS.setdefault(uid, asyncio.Lock()):
            ops.clear_state(uid)
            await chat.send_message('منوی اصلی:', reply_markup=ops.main_menu())
        return True
    async with LOCKS.setdefault(uid, asyncio.Lock()):
        state = live_state(uid, chat.id)
        if not state:
            await begin(update)
            return True
        d = state['data']
        try:
            if state['step'] == 'quantity':
                n = quantity(value)
                p = d['selected']
                d.update(amount=n, request_key=secrets.token_hex(16))
                session(uid, 'confirm', d)
                await chat.send_message(
                    describe(p) + f"\nتعداد ورودی: {n}\nموجودی پس از شارژ: {p['quantity'] + n}\n"
                    'آیا همین محصول و تعداد صحیح است؟',
                    reply_markup=keyboard([[('✅ تأیید شارژ', f"stock:apply:{d['token']}")],
                                           [('لغو و جست‌وجوی جدید', f"stock:reset:{d['token']}")]]))
            elif state['step'] == 'confirm':
                await chat.send_message('برای ثبت، دکمه تأیید را بزنید یا عملیات را لغو کنید.')
            else:
                if not 2 <= len(value) <= 100:
                    raise ValueError('حداقل دو حرف از نام یا کد کالا را وارد کنید.')
                await search(chat, uid, d, value)
        except ValueError as exc:
            await chat.send_message(str(exc))
        except Exception:
            await chat.send_message('ارتباط با موجودی سایت برقرار نشد. هیچ شارژی ثبت نشده است.')
    return True


async def callback(update, ctx):
    q = update.callback_query
    parts = (q.data or '').split(':')
    if parts[0] != 'stock':
        return False
    uid, chat = q.from_user.id, update.effective_chat
    if chat.type != 'private' or not m.allowed(uid):
        await q.answer('دسترسی ندارید', show_alert=True)
        return True
    async with LOCKS.setdefault(uid, asyncio.Lock()):
        state = live_state(uid, chat.id)
        if not state or len(parts) < 3 or parts[2] != state['data'].get('token'):
            await q.answer('این انتخاب منقضی شده است؛ دوباره جست‌وجو کنید.', show_alert=True)
            return True
        await q.answer()
        d, action = state['data'], parts[1]
        try:
            if action == 'reset':
                await begin(update)
            elif action == 'next' and state['step'] == 'choose':
                d['page'] += 1
                await cards(chat, uid, d, await call('stock_search', {
                    'q': d['query'], 'parent': d['parent'], 'page': d['page']}))
            elif action == 'pick' and state['step'] == 'choose' and len(parts) == 4:
                selected = d['items'].get(parts[3])
                if not selected:
                    return True
                p = (await call('stock_detail', {'id': selected['id']}))['product']
                if p['type'] == 'variable':
                    d.update(parent=p['id'], page=1)
                    await cards(chat, uid, d, await call('stock_search', {'parent': p['id'], 'page': 1}))
                elif not p.get('receivable'):
                    await chat.send_message('موجودی مستقل و قابل شارژ برای این کالا مشخص نیست. مدل یا تنظیمات انبار را بررسی کنید.')
                else:
                    d.update(selected=p, token=secrets.token_hex(6))
                    session(uid, 'quantity', d)
                    await chat.send_message(describe(p) + '\nچند عدد به موجودی اضافه شود؟')
            elif action == 'apply' and state['step'] == 'confirm':
                p = d['selected']
                payload = {'id': p['id'], 'identity': p['identity'], 'expected': p['quantity'],
                           'amount': d['amount'], 'request_key': d['request_key'], 'actor': str(uid)}
                # Consume the UI selection before the write. The same request key remains
                # available for read-only recovery, never for an automatic second write.
                session(uid, 'checking', d)
                try:
                    result = await call('stock_receive', payload)
                except Exception:
                    try:
                        result = await call('stock_receipt_status', {'request_key': d['request_key']})
                    except Exception:
                        result = {}
                if result.get('status') == 'applied':
                    await chat.send_message(
                        f"✅ شارژ ثبت شد.\n{p['name']}\nورودی: {d['amount']} عدد\n"
                        f"موجودی ثبت‌شده: {result['quantity']}\n"
                        'برای کالای بعدی نام یا کد آن را بفرستید.')
                    session(uid, 'search', {'chat': chat.id})
                else:
                    await chat.send_message(
                        '⚠️ نتیجه شارژ قطعی نیست یا موجودی تغییر کرده است. '
                        f"شناسه پیگیری: {d['request_key']}\n"
                        'برای جلوگیری از شارژ دوباره ابتدا موجودی و سابقه عملیات را بررسی کنید.')
                    session(uid, 'checking', d)
        except Exception:
            await chat.send_message('عملیات تکمیل نشد. دوباره از منوی شارژ موجودی شروع کنید.')
    return True
