import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update

from app import main as m

ORIG_START = m.start
ORIG_TEXT = m.text
ORIG_CALLBACK = m.callback

with m.db() as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS op_state(
        user INTEGER PRIMARY KEY,
        flow TEXT NOT NULL,
        step TEXT NOT NULL,
        data TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS woo_config(
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
    );
    ''')


def cfg_get(key, default=None):
    with m.db() as c:
        r = c.execute('SELECT v FROM woo_config WHERE k=?', (key,)).fetchone()
        return r['v'] if r else default


def cfg_set(key, value):
    with m.db() as c:
        c.execute(
            'INSERT INTO woo_config(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
            (key, str(value)),
        )


def get_state(uid):
    with m.db() as c:
        r = c.execute('SELECT * FROM op_state WHERE user=?', (uid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d['data'] = json.loads(d['data'] or '{}')
        return d


def set_state(uid, flow, step, data=None):
    with m.db() as c:
        c.execute(
            '''INSERT INTO op_state(user,flow,step,data) VALUES(?,?,?,?)
               ON CONFLICT(user) DO UPDATE SET flow=excluded.flow,step=excluded.step,data=excluded.data''',
            (uid, flow, step, json.dumps(data or {}, ensure_ascii=False)),
        )


def update_state(uid, *, step=None, data=None):
    s = get_state(uid)
    if not s:
        return
    set_state(uid, s['flow'], step or s['step'], data if data is not None else s['data'])


def clear_state(uid):
    with m.db() as c:
        c.execute('DELETE FROM op_state WHERE user=?', (uid,))


def main_menu():
    return ReplyKeyboardMarkup([
        ['📦 رهگیری شاپینو', '🛍 مدیریت ووکامرس'],
        ['⚙️ تنظیمات و اتصال‌ها', '👥 کاربران و دسترسی'],
    ], resize_keyboard=True)


def shopino_menu():
    return ReplyKeyboardMarkup([
        ['📤 ارسال فایل رهگیری', '🔐 ورود شاپینو'],
        ['📊 وضعیت شاپینو', '⬅️ منوی اصلی'],
    ], resize_keyboard=True)


def woo_menu():
    return ReplyKeyboardMarkup([
        ['➕ ثبت محصول جدید', '📋 محصولات اخیر'],
        ['🔌 اتصال ووکامرس', '⬅️ منوی اصلی'],
    ], resize_keyboard=True)


def settings_menu():
    return ReplyKeyboardMarkup([
        ['🔐 ورود شاپینو', '🔌 اتصال ووکامرس'],
        ['📊 وضعیت اتصال‌ها', '⬅️ منوی اصلی'],
    ], resize_keyboard=True)


def cancel_menu():
    return ReplyKeyboardMarkup([['❌ لغو عملیات', '⬅️ منوی اصلی']], resize_keyboard=True)


class WooClient:
    def __init__(self):
        self.url = (cfg_get('url') or '').rstrip('/')
        self.ck = cfg_get('ck') or ''
        self.cs = cfg_get('cs') or ''
        self.wp_user = cfg_get('wp_user') or ''
        self.wp_app_password = cfg_get('wp_app_password') or ''
        if not self.url or not self.ck or not self.cs:
            raise RuntimeError('اتصال ووکامرس تنظیم نشده است. از «🔌 اتصال ووکامرس» استفاده کنید.')
        self.c = httpx.Client(timeout=httpx.Timeout(60.0, connect=20.0), follow_redirects=True)

    def wc(self, method, path, **kwargs):
        url = f'{self.url}/wp-json/wc/v3/{path.lstrip("/")}'
        params = dict(kwargs.pop('params', {}) or {})
        # Basic auth is preferred on HTTPS; query credentials are a compatibility fallback.
        r = self.c.request(method, url, auth=(self.ck, self.cs), params=params, **kwargs)
        if r.status_code in (401, 403):
            params.update({'consumer_key': self.ck, 'consumer_secret': self.cs})
            r = self.c.request(method, url, params=params, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f'WooCommerce HTTP {r.status_code}: {r.text[:350]}')
        return r

    def probe(self):
        r = self.wc('GET', 'products', params={'per_page': 1})
        return r.headers.get('x-wp-total', '?')

    def categories(self):
        page = 1
        out = []
        while page <= 10:
            r = self.wc('GET', 'products/categories', params={'per_page': 100, 'page': page, 'hide_empty': False})
            batch = r.json()
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        out.sort(key=lambda x: (int(x.get('parent') or 0), str(x.get('name') or '')))
        return out

    def recent_products(self):
        return self.wc('GET', 'products', params={'per_page': 10, 'orderby': 'date', 'order': 'desc'}).json()

    def upload_media(self, path, filename):
        if not self.wp_user or not self.wp_app_password:
            raise RuntimeError('برای ارسال عکس از تلگرام باید WP Username و Application Password هم تنظیم شود.')
        url = f'{self.url}/wp-json/wp/v2/media'
        mime = 'image/jpeg'
        ext = Path(filename).suffix.lower()
        if ext == '.png':
            mime = 'image/png'
        with open(path, 'rb') as f:
            r = self.c.post(
                url,
                auth=(self.wp_user, self.wp_app_password),
                headers={'Content-Disposition': f'attachment; filename="{filename}"', 'Content-Type': mime},
                content=f.read(),
            )
        if r.status_code >= 400:
            raise RuntimeError(f'WordPress Media HTTP {r.status_code}: {r.text[:350]}')
        return r.json()

    def create_product(self, data):
        return self.wc('POST', 'products', json=data).json()


def type_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('📦 ثابت (Simple)', callback_data='woo:type:simple'),
        InlineKeyboardButton('🎛 متغیر (Variable)', callback_data='woo:type:variable'),
    ]])


def photos_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('✅ تصاویر تمام شد', callback_data='woo:photos:done'),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


def sale_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('بدون تخفیف', callback_data='woo:sale:skip'),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


def categories_keyboard(categories, selected, page=0):
    per = 8
    pages = max(1, (len(categories) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    rows = []
    for cat in categories[page * per:(page + 1) * per]:
        cid = int(cat['id'])
        mark = '✅' if cid in selected else '▫️'
        rows.append([InlineKeyboardButton(f'{mark} {cat["name"]}', callback_data=f'woo:cat:{cid}:{page}')])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton('◀️', callback_data=f'woo:catpage:{page-1}'))
    nav.append(InlineKeyboardButton(f'{page+1}/{pages}', callback_data='woo:noop'))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton('▶️', callback_data=f'woo:catpage:{page+1}'))
    rows.append(nav)
    rows.append([InlineKeyboardButton('✅ تأیید دسته‌بندی‌ها', callback_data='woo:catdone')])
    return InlineKeyboardMarkup(rows)


def preview_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('🚀 ثبت در سایت', callback_data='woo:publish'),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


def money(v):
    return f'{v:,}' if isinstance(v, int) else str(v)


async def start(update, ctx):
    if not await m.access(update):
        return
    clear_state(update.effective_user.id)
    await update.message.reply_text(
        'پنل عملیاتی Vesta آماده است. از منوی زیر بخش موردنظر را انتخاب کنید.',
        reply_markup=main_menu(),
    )


async def show_woo_status(chat):
    try:
        total = await asyncio.to_thread(WooClient().probe)
        await chat.send_message(f'✅ اتصال ووکامرس برقرار است. تعداد محصولات: {total}')
    except Exception as exc:
        await chat.send_message(f'❌ ووکامرس: {exc}')


async def begin_woo_setup(update):
    uid = update.effective_user.id
    set_state(uid, 'woo_setup', 'url', {})
    await update.message.reply_text(
        '🔌 اتصال ووکامرس\n\nآدرس سایت را بفرستید؛ مثال:\nhttps://vestacosmetics.com',
        reply_markup=cancel_menu(),
    )


async def setup_text(update, state):
    uid = update.effective_user.id
    value = (update.message.text or '').strip()
    data = state['data']
    step = state['step']

    if step == 'url':
        if not value.startswith('http'):
            return await update.message.reply_text('آدرس باید با http:// یا https:// شروع شود.')
        data['url'] = value.rstrip('/')
        update_state(uid, step='ck', data=data)
        return await update.message.reply_text('Consumer Key ووکامرس (ck_...) را بفرستید.')
    if step == 'ck':
        data['ck'] = value
        update_state(uid, step='cs', data=data)
        try:
            await update.message.delete()
        except Exception:
            pass
        return await update.effective_chat.send_message('Consumer Secret ووکامرس (cs_...) را بفرستید.')
    if step == 'cs':
        data['cs'] = value
        update_state(uid, step='wp_user', data=data)
        try:
            await update.message.delete()
        except Exception:
            pass
        return await update.effective_chat.send_message(
            'نام کاربری WordPress را بفرستید.\nاین مورد برای آپلود مستقیم عکس‌های تلگرام به Media Library لازم است.'
        )
    if step == 'wp_user':
        data['wp_user'] = value
        update_state(uid, step='wp_app_password', data=data)
        return await update.message.reply_text('Application Password وردپرس را بفرستید.')
    if step == 'wp_app_password':
        data['wp_app_password'] = value
        for k in ('url', 'ck', 'cs', 'wp_user', 'wp_app_password'):
            cfg_set(k, data.get(k, ''))
        clear_state(uid)
        try:
            await update.message.delete()
        except Exception:
            pass
        try:
            total = await asyncio.to_thread(WooClient().probe)
            return await update.effective_chat.send_message(
                f'✅ اتصال ووکامرس ذخیره و تست شد. {total} محصول قابل مشاهده است.',
                reply_markup=woo_menu(),
            )
        except Exception as exc:
            return await update.effective_chat.send_message(
                f'⚠️ اطلاعات ذخیره شد ولی تست اتصال موفق نبود:\n{exc}\n\nمی‌توانید دوباره «🔌 اتصال ووکامرس» را بزنید.',
                reply_markup=woo_menu(),
            )


async def begin_product(update):
    try:
        await asyncio.to_thread(WooClient().probe)
    except Exception as exc:
        return await update.message.reply_text(f'اول اتصال ووکامرس را تنظیم کنید:\n{exc}', reply_markup=woo_menu())
    uid = update.effective_user.id
    set_state(uid, 'woo_product', 'name', {'images': [], 'categories': [], 'category_page': 0})
    await update.message.reply_text('➕ ثبت محصول جدید\n\nاسم محصول را بفرستید.', reply_markup=cancel_menu())


async def product_text(update, state):
    uid = update.effective_user.id
    text = (update.message.text or '').strip()
    data = state['data']
    step = state['step']

    if step == 'name':
        data['name'] = text
        update_state(uid, step='type', data=data)
        return await update.message.reply_text('نوع محصول را انتخاب کنید:', reply_markup=type_keyboard())
    if step == 'stock':
        x = text.translate(m.DIGITS).replace(',', '').strip()
        if not x.isdigit():
            return await update.message.reply_text('موجودی را فقط به صورت عدد وارد کنید.')
        data['stock'] = int(x)
        update_state(uid, step='regular_price', data=data)
        return await update.message.reply_text('قیمت اصلی را به عدد بفرستید. مثال: 1250000')
    if step == 'regular_price':
        x = text.translate(m.DIGITS).replace(',', '').strip()
        if not x.isdigit():
            return await update.message.reply_text('قیمت را فقط به صورت عدد وارد کنید.')
        data['regular_price'] = x
        update_state(uid, step='sale_price', data=data)
        return await update.message.reply_text(
            'قیمت با تخفیف را بفرستید؛ اگر تخفیف ندارد دکمه «بدون تخفیف» را بزنید.',
            reply_markup=sale_keyboard(),
        )
    if step == 'sale_price':
        x = text.translate(m.DIGITS).replace(',', '').strip()
        if not x.isdigit():
            return await update.message.reply_text('قیمت تخفیف را فقط به صورت عدد وارد کنید.')
        if int(x) >= int(data['regular_price']):
            return await update.message.reply_text('قیمت تخفیف باید از قیمت اصلی کمتر باشد.')
        data['sale_price'] = x
        update_state(uid, step='categories', data=data)
        return await send_categories(update.effective_chat, uid)

    return await update.message.reply_text('برای ادامه از دکمه‌های همین مرحله استفاده کنید.')


async def send_categories(chat, uid, edit_message=None):
    state = get_state(uid)
    if not state:
        return
    data = state['data']
    try:
        cats = await asyncio.to_thread(WooClient().categories)
    except Exception as exc:
        return await chat.send_message(f'❌ دریافت دسته‌بندی‌ها ناموفق بود: {exc}')
    data['category_cache'] = [{'id': int(x['id']), 'name': x['name']} for x in cats]
    update_state(uid, step='categories', data=data)
    kb = categories_keyboard(data['category_cache'], set(data.get('categories', [])), data.get('category_page', 0))
    txt = 'دسته‌بندی‌های محصول را انتخاب کنید. می‌توانید چند مورد را تیک بزنید:'
    if edit_message:
        await edit_message.edit_message_text(txt, reply_markup=kb)
    else:
        await chat.send_message(txt, reply_markup=kb)


async def photo(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    state = get_state(uid)
    if not state or state['flow'] != 'woo_product' or state['step'] != 'photos':
        return
    data = state['data']
    p = update.message.photo[-1]
    f = await p.get_file(read_timeout=45, connect_timeout=30)
    tmp = Path(tempfile.gettempdir()) / f'woo_{uuid.uuid4().hex}.jpg'
    try:
        await f.download_to_drive(custom_path=str(tmp), read_timeout=90, connect_timeout=30)
        media = await asyncio.to_thread(WooClient().upload_media, tmp, tmp.name)
        data.setdefault('images', []).append({'id': int(media['id']), 'src': media.get('source_url', '')})
        update_state(uid, data=data)
        n = len(data['images'])
        role = 'عکس اصلی' if n == 1 else f'گالری #{n-1}'
        await update.message.reply_text(
            f'✅ {role} دریافت و روی سایت آپلود شد.\n'
            f'تعداد تصاویر فعلی: {n}\n\n'
            'عکس بعدی را بفرستید یا «تصاویر تمام شد» را بزنید.',
            reply_markup=photos_keyboard(),
        )
    except Exception as exc:
        await update.message.reply_text(f'❌ آپلود عکس ناموفق بود: {exc}')
    finally:
        tmp.unlink(missing_ok=True)


async def product_preview(chat, uid):
    state = get_state(uid)
    data = state['data']
    cats = {x['id']: x['name'] for x in data.get('category_cache', [])}
    selected = [cats.get(i, str(i)) for i in data.get('categories', [])]
    typ = 'متغیر' if data.get('type') == 'variable' else 'ثابت'
    sale = data.get('sale_price') or '—'
    text = (
        '🧾 پیش‌نمایش محصول\n\n'
        f'نام: {data.get("name")}\n'
        f'نوع: {typ}\n'
        f'موجودی: {data.get("stock")}\n'
        f'قیمت اصلی: {data.get("regular_price")}\n'
        f'قیمت تخفیف: {sale}\n'
        f'تصاویر: {len(data.get("images", []))} (اولی اصلی)\n'
        f'دسته‌بندی: {"، ".join(selected) if selected else "بدون دسته"}\n\n'
        'اگر اطلاعات درست است ثبت نهایی را بزنید.'
    )
    await chat.send_message(text, reply_markup=preview_keyboard())


async def publish_product(chat, uid):
    state = get_state(uid)
    data = state['data']
    payload = {
        'name': data['name'],
        'type': data.get('type', 'simple'),
        'status': 'publish',
        'categories': [{'id': int(i)} for i in data.get('categories', [])],
        'images': [{'id': int(x['id'])} for x in data.get('images', [])],
    }
    # For now variable products are created as a variable parent. Variation attributes/pricing
    # will be collected in the next module; simple products are fully publishable now.
    if payload['type'] == 'simple':
        payload.update({
            'manage_stock': True,
            'stock_quantity': int(data.get('stock', 0)),
            'regular_price': str(data.get('regular_price', '')),
        })
        if data.get('sale_price'):
            payload['sale_price'] = str(data['sale_price'])
    else:
        payload['manage_stock'] = True
        payload['stock_quantity'] = int(data.get('stock', 0))

    product = await asyncio.to_thread(WooClient().create_product, payload)
    clear_state(uid)
    await chat.send_message(
        f'✅ محصول ثبت شد.\n\n#{product.get("id")} — {product.get("name")}\n{product.get("permalink", "")}',
        reply_markup=woo_menu(),
    )


async def callback(update, ctx):
    q = update.callback_query
    data_cb = q.data or ''
    if not data_cb.startswith('woo:'):
        return await ORIG_CALLBACK(update, ctx)
    if not m.allowed(q.from_user.id):
        return await q.answer('دسترسی ندارید', show_alert=True)
    await q.answer()
    uid = q.from_user.id
    state = get_state(uid)

    if data_cb == 'woo:noop':
        return
    if data_cb == 'woo:cancel':
        clear_state(uid)
        return await q.edit_message_text('❌ عملیات لغو شد.')
    if not state or state['flow'] != 'woo_product':
        return await q.edit_message_text('این عملیات منقضی شده؛ دوباره از منوی ووکامرس شروع کنید.')

    d = state['data']
    if data_cb.startswith('woo:type:'):
        typ = data_cb.split(':')[2]
        d['type'] = typ
        update_state(uid, step='photos', data=d)
        return await q.edit_message_text(
            '📸 تصاویر محصول را بفرستید.\n\nاولین عکس = تصویر اصلی محصول\nعکس‌های بعدی = گالری\n\nوقتی تمام شد دکمه زیر را بزنید.',
            reply_markup=photos_keyboard(),
        )
    if data_cb == 'woo:photos:done':
        if not d.get('images'):
            return await q.answer('حداقل یک عکس لازم است.', show_alert=True)
        update_state(uid, step='stock', data=d)
        await q.edit_message_text(f'✅ {len(d["images"])} تصویر ثبت شد.')
        return await q.message.reply_text('موجودی محصول چند عدد است؟')
    if data_cb == 'woo:sale:skip':
        d['sale_price'] = ''
        update_state(uid, step='categories', data=d)
        await q.edit_message_text('بدون تخفیف ثبت شد.')
        return await send_categories(q.message.chat, uid)
    if data_cb.startswith('woo:cat:'):
        _, _, sid, spage = data_cb.split(':')
        cid = int(sid)
        selected = set(d.get('categories', []))
        if cid in selected:
            selected.remove(cid)
        else:
            selected.add(cid)
        d['categories'] = sorted(selected)
        d['category_page'] = int(spage)
        update_state(uid, data=d)
        kb = categories_keyboard(d.get('category_cache', []), selected, int(spage))
        return await q.edit_message_reply_markup(reply_markup=kb)
    if data_cb.startswith('woo:catpage:'):
        page = int(data_cb.split(':')[2])
        d['category_page'] = page
        update_state(uid, data=d)
        kb = categories_keyboard(d.get('category_cache', []), set(d.get('categories', [])), page)
        return await q.edit_message_reply_markup(reply_markup=kb)
    if data_cb == 'woo:catdone':
        update_state(uid, step='preview', data=d)
        await q.edit_message_text('✅ دسته‌بندی‌ها انتخاب شدند.')
        return await product_preview(q.message.chat, uid)
    if data_cb == 'woo:publish':
        try:
            await q.edit_message_text('⏳ در حال ثبت محصول در ووکامرس…')
            return await publish_product(q.message.chat, uid)
        except Exception as exc:
            return await q.message.reply_text(f'❌ ثبت محصول ناموفق بود: {exc}')


async def text(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    text_value = (update.message.text or '').strip()

    if text_value in {'❌ لغو عملیات', '⬅️ منوی اصلی'}:
        clear_state(uid)
        return await update.message.reply_text('منوی اصلی:', reply_markup=main_menu())

    state = get_state(uid)
    if state:
        if state['flow'] == 'woo_setup':
            return await setup_text(update, state)
        if state['flow'] == 'woo_product':
            return await product_text(update, state)

    if text_value == '📦 رهگیری شاپینو':
        return await update.message.reply_text('بخش رهگیری شاپینو:', reply_markup=shopino_menu())
    if text_value == '🛍 مدیریت ووکامرس':
        return await update.message.reply_text('مدیریت ووکامرس:', reply_markup=woo_menu())
    if text_value == '⚙️ تنظیمات و اتصال‌ها':
        return await update.message.reply_text('تنظیمات و اتصال‌ها:', reply_markup=settings_menu())
    if text_value == '👥 کاربران و دسترسی':
        return await update.message.reply_text(
            'برای دیدن کاربران /users و برای اضافه‌کردن ادمین /allow TELEGRAM_ID را بزنید.',
            reply_markup=main_menu(),
        )
    if text_value == '📤 ارسال فایل رهگیری':
        return await update.message.reply_text('فایل xlsx/xlsm پست را همینجا ارسال کنید.', reply_markup=shopino_menu())
    if text_value == '🔐 ورود شاپینو':
        return await m.login_cmd(update, ctx)
    if text_value == '📊 وضعیت شاپینو':
        return await m.status(update, ctx)
    if text_value == '🔌 اتصال ووکامرس':
        return await begin_woo_setup(update)
    if text_value == '➕ ثبت محصول جدید':
        return await begin_product(update)
    if text_value == '📋 محصولات اخیر':
        try:
            products = await asyncio.to_thread(WooClient().recent_products)
            if not products:
                return await update.message.reply_text('محصولی پیدا نشد.', reply_markup=woo_menu())
            lines = ['📋 ۱۰ محصول اخیر:']
            for p in products:
                lines.append(f'• #{p.get("id")} {p.get("name")} — موجودی: {p.get("stock_quantity")} — {p.get("price") or "بدون قیمت"}')
            return await update.message.reply_text('\n'.join(lines), reply_markup=woo_menu())
        except Exception as exc:
            return await update.message.reply_text(f'❌ {exc}', reply_markup=woo_menu())
    if text_value == '📊 وضعیت اتصال‌ها':
        try:
            scount = await asyncio.to_thread(m.api().probe)
            shop = f'✅ شاپینو: {scount} سفارش'
        except Exception as exc:
            shop = f'❌ شاپینو: {exc}'
        try:
            pcount = await asyncio.to_thread(WooClient().probe)
            woo = f'✅ ووکامرس: {pcount} محصول'
        except Exception as exc:
            woo = f'❌ ووکامرس: {exc}'
        return await update.message.reply_text(f'{shop}\n{woo}', reply_markup=settings_menu())

    return await ORIG_TEXT(update, ctx)


# Runner imports this module and applies these wrappers before core.main() builds handlers.
