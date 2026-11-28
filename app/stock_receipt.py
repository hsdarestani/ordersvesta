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


def price(value):
    raw = str(value).translate(m.DIGITS).strip()
    # Accept grouped thousands, not decimal or ambiguous punctuation.
    if re.fullmatch(r'[0-9]{1,3}(?:[,٬][0-9]{3})+', raw):
        raw = raw.replace(',', '').replace('٬', '')
    if not re.fullmatch(r'[1-9][0-9]{0,11}', raw):
        raise ValueError('قیمت را به صورت عدد صحیح مثبت وارد کنید.')
    return raw


def currency(p):
    return {'IRT': 'تومان', 'IRR': 'ریال', 'IRHT': 'هزار تومان', 'IRHR': 'هزار ریال'}.get(
        p.get('currency'), p.get('currency') or 'واحد سایت')


def fmt_price(value, p):
    if value is None or value == '':
        return 'ثبت نشده'
    try:
        from decimal import Decimal
        n = Decimal(str(value))
        shown = f'{n:,.0f}' if n == n.to_integral_value() else str(n)
        return shown + ' ' + currency(p)
    except Exception:
        return str(value) + ' ' + currency(p)


async def confirm(chat, uid, d):
    p = d['selected']
    session(uid, 'confirm', d)
    change = 'قیمت بدون تغییر'
    if d.get('price_mode'):
        label = 'قیمت اصلی' if d['price_mode'] == 'regular' else 'قیمت تخفیف'
        change = label + ' جدید: ' + fmt_price(d['new_price'], p)
    rows = [[('✅ تأیید نهایی', f"stock:apply:{d['token']}")]]
    if p.get('price_enabled') and p.get('price_version'):
        rows.append([('✏️ تغییر قیمت', f"stock:editprice:{d['token']}")])
    rows.append([('لغو و جست‌وجوی جدید', f"stock:reset:{d['token']}")])
    await chat.send_message(
        describe(p) + f"\nتعداد ورودی: {d['amount']}\nموجودی پس از شارژ: {p['quantity'] + d['amount']}\n{change}\n"
        'اطلاعات را بررسی و تأیید کنید.',
        reply_markup=keyboard(rows))


async def price_choices(chat, uid, d):
    p = d['selected']
    session(uid, 'price_choice', d)
    rows = [[('✏️ تغییر قیمت اصلی', f"stock:regular:{d['token']}")]]
    if p.get('sale_price') not in ('', None):
        rows.append([('✏️ تغییر قیمت تخفیف', f"stock:sale:{d['token']}")])
    rows.append([('بدون تغییر قیمت', f"stock:keepprice:{d['token']}")])
    await chat.send_message('قیمت را هم تغییر می‌دهید؟\n' + describe(p),
                            reply_markup=keyboard(rows))


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
        f"موجودی فعلی: {p.get('quantity') if p.get('quantity') is not None else 'نامشخص'}\n"
        + (f"قیمت اصلی: {fmt_price(p.get('regular_price'), p)}\n"
           f"قیمت تخفیف: {fmt_price(p.get('sale_price'), p)}\n"
           f"قیمت فعلی فروش: {fmt_price(p.get('current_price'), p)}" if p.get('price_enabled') else '')
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
                previous = d['selected']
                # Re-read the live product rather than using pre-deployment SQLite state.
                p = (await call('stock_detail', {'id': previous['id']}))['product']
                if p['identity'] != previous['identity'] or p['quantity'] != previous['quantity'] or not p.get('receivable'):
                    await chat.send_message('موجودی یا مشخصات کالا تغییر کرده است. دوباره محصول را انتخاب کنید.')
                    await begin(update)
                    return True
                if not p.get('price_enabled') or not p.get('price_version'):
                    await chat.send_message('اطلاعات قیمت از سایت دریافت نشد. ثبت انجام نشد؛ دوباره تلاش کنید.')
                    return True
                d.update(selected=p, amount=n, request_key=secrets.token_hex(16), token=secrets.token_hex(6))
                d.pop('price_mode', None)
                d.pop('new_price', None)
                await price_choices(chat, uid, d)
            elif state['step'] == 'price':
                n = price(value)
                p = d['selected']
                if d['price_mode'] == 'regular' and p.get('sale_price') not in ('', None) and float(n) <= float(p['sale_price']):
                    raise ValueError('قیمت اصلی باید بیشتر از قیمت تخفیف فعلی باشد.')
                if d['price_mode'] == 'sale' and float(n) >= float(p['regular_price']):
                    raise ValueError('قیمت تخفیف باید کمتر از قیمت اصلی باشد.')
                d['new_price'] = n
                await confirm(chat, uid, d)
            elif state['step'] == 'price_choice':
                await chat.send_message('یکی از گزینه‌های تغییر یا حفظ قیمت را انتخاب کنید.')
            elif state['step'] == 'checking':
                await chat.send_message('نتیجه ثبت قبلی هنوز نیاز به بررسی دارد. شارژ را تکرار نکنید.')
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
            elif action == 'editprice' and state['step'] == 'confirm':
                p = (await call('stock_detail', {'id': d['selected']['id']}))['product']
                if p['identity'] != d['selected']['identity'] or p['quantity'] != d['selected']['quantity']:
                    await chat.send_message('موجودی یا مشخصات کالا تغییر کرده است. دوباره جست‌وجو کنید.')
                    return True
                if not p.get('price_enabled') or not p.get('price_version'):
                    await chat.send_message('اطلاعات قیمت از سایت دریافت نشد.')
                    return True
                d.update(selected=p, token=secrets.token_hex(6))
                await price_choices(chat, uid, d)
            elif action == 'keepprice' and state['step'] == 'price_choice':
                d.pop('price_mode', None)
                d.pop('new_price', None)
                await confirm(chat, uid, d)
            elif action in {'regular', 'sale'} and state['step'] == 'price_choice':
                p = d['selected']
                if action == 'sale' and p.get('sale_price') in ('', None):
                    return True
                d['price_mode'] = action
                session(uid, 'price', d)
                await chat.send_message('قیمت جدید را به ' + currency(p) + ' وارد کنید (عدد صحیح).')
            elif action == 'apply' and state['step'] == 'confirm':
                p = d['selected']
                payload = {'id': p['id'], 'identity': p['identity'], 'expected': p['quantity'],
                           'amount': d['amount'], 'request_key': d['request_key'], 'actor': str(uid)}
                operation = 'stock_receive'
                if d.get('price_mode'):
                    operation = 'stock_receive_price'
                    payload.update(price_mode=d['price_mode'], new_price=d['new_price'],
                                   price_version=p['price_version'])
                # Consume the UI selection before the write. The same request key remains
                # available for read-only recovery, never for an automatic second write.
                session(uid, 'checking', d)
                try:
                    result = await call(operation, payload)
                except Exception:
                    try:
                        result = await call('stock_receipt_status', {'request_key': d['request_key']})
                    except Exception:
                        result = {}
                if result.get('status') == 'applied':
                    price_result = result.get('price_result')
                    price_line = ''
                    if price_result:
                        label = 'قیمت اصلی' if price_result['mode'] == 'regular' else 'قیمت تخفیف'
                        price_line = f"{label} ثبت‌شده: {fmt_price(price_result['value'], p)}\n"
                    await chat.send_message(
                        f"✅ شارژ ثبت شد.\n{p['name']}\nورودی: {d['amount']} عدد\n"
                        f"موجودی ثبت‌شده: {result['quantity']}\n" + price_line +
                        'برای کالای بعدی نام یا کد آن را بفرستید.')
                    session(uid, 'search', {'chat': chat.id})
                elif result.get('status') in {'stock_applied_price_pending', 'stock_applied_price_unverified'}:
                    await chat.send_message(
                        f"⚠️ موجودی ثبت شد: {result.get('quantity')} عدد.\n"
                        'تغییر قیمت هنوز تأیید نشده؛ شارژ را تکرار نکنید.\n'
                        f"شناسه پیگیری: {d['request_key']}")
                    session(uid, 'checking', d)
                else:
                    await chat.send_message(
                        '⚠️ نتیجه شارژ قطعی نیست یا موجودی تغییر کرده است. '
                        f"شناسه پیگیری: {d['request_key']}\n"
                        'برای جلوگیری از شارژ دوباره ابتدا موجودی و سابقه عملیات را بررسی کنید.')
                    session(uid, 'checking', d)
        except Exception:
            await chat.send_message('عملیات تکمیل نشد. دوباره از منوی شارژ موجودی شروع کنید.')
    return True
