import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
import threading
import unicodedata
import uuid
import zipfile
from difflib import SequenceMatcher
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

TOKEN = os.environ['BOTTOKEN']
SHOP = os.getenv('SHOP_ID', '7944')
BASE = 'https://api.shopino.app/api/v1/shop-panel'
SEND_CODE_URL = 'https://api-go.shopino.app/api/v1/app/send-verification-code/'
VERIFY_PHONE_URL = 'https://api.shopino.app/api/v1/app/verify-phone/'
DATA = Path(os.getenv('DATA_DIR', '/app/data'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'state.db'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('ordersvesta')

WEB_HEADERS = {
    'accept': 'application/json',
    'content-type': 'application/json',
    'origin': 'https://panel.shopino.app',
    'referer': 'https://panel.shopino.app/',
    'user-agent': 'Mozilla/5.0 (OrdersVestaBot; Shopino panel integration)',
}


# ---------- persistent state ----------
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY);
    CREATE TABLE IF NOT EXISTS pending(
        token TEXT PRIMARY KEY,chat INTEGER,user INTEGER,payload TEXT,status TEXT DEFAULT 'pending'
    );
    CREATE TABLE IF NOT EXISTS manual(user INTEGER PRIMARY KEY,token TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS shopino_login(
        user INTEGER PRIMARY KEY,step TEXT NOT NULL,phone TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    ''')


def get(k):
    with db() as c:
        r = c.execute('SELECT v FROM kv WHERE k=?', (k,)).fetchone()
        return r['v'] if r else None


def setv(k, v):
    with db() as c:
        c.execute(
            'INSERT INTO kv VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',
            (k, str(v)),
        )


def allowed(uid):
    if get('owner') == str(uid):
        return True
    with db() as c:
        return c.execute('SELECT 1 FROM users WHERE id=?', (uid,)).fetchone() is not None


def add_user(uid):
    with db() as c:
        c.execute('INSERT OR IGNORE INTO users VALUES(?)', (uid,))


def save_pending(chat, user, payload):
    t = uuid.uuid4().hex[:10]
    with db() as c:
        c.execute(
            'INSERT INTO pending(token,chat,user,payload) VALUES(?,?,?,?)',
            (t, chat, user, json.dumps(payload, ensure_ascii=False)),
        )
    return t


def pending(t):
    with db() as c:
        r = c.execute('SELECT * FROM pending WHERE token=?', (t,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d['payload'] = json.loads(d['payload'])
        return d


def done(t, status='done'):
    with db() as c:
        c.execute('UPDATE pending SET status=? WHERE token=?', (status, t))
        c.execute('DELETE FROM manual WHERE token=?', (t,))


def login_state(uid):
    with db() as c:
        r = c.execute('SELECT * FROM shopino_login WHERE user=?', (uid,)).fetchone()
        return dict(r) if r else None


def set_login_state(uid, step, phone=''):
    with db() as c:
        c.execute(
            '''INSERT INTO shopino_login(user,step,phone) VALUES(?,?,?)
               ON CONFLICT(user) DO UPDATE SET step=excluded.step,phone=excluded.phone,created_at=CURRENT_TIMESTAMP''',
            (uid, step, phone),
        )


def clear_login_state(uid):
    with db() as c:
        c.execute('DELETE FROM shopino_login WHERE user=?', (uid,))


# ---------- Excel OOXML (.xlsx/.xlsm) ----------
TR = str.maketrans({
    'ي': 'ی', 'ى': 'ی', 'ئ': 'ی', 'ك': 'ک', 'ة': 'ه', 'ۀ': 'ه', 'ؤ': 'و',
    'إ': 'ا', 'أ': 'ا', 'ٱ': 'ا', 'آ': 'ا',
})
DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def norm(x, compact=False):
    s = unicodedata.normalize('NFKC', str(x or '')).translate(TR)
    s = s.replace('\u200c', ' ').replace('\u200f', ' ')
    s = re.sub(r'[^0-9A-Za-zآ-ی\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s.replace(' ', '') if compact else s


def normalize_phone(x):
    s = str(x or '').translate(DIGITS)
    s = re.sub(r'\D', '', s)
    if s.startswith('98') and len(s) == 12:
        s = '0' + s[2:]
    return s


def col(ref):
    m = re.match(r'[A-Z]+', ref or '')
    n = 0
    if not m:
        return -1
    for ch in m.group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def ooxml_rows(path):
    ns = {
        'm': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main',
        'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
        'p': 'http://schemas.openxmlformats.org/package/2006/relationships',
    }
    with zipfile.ZipFile(path) as z:
        shared = []
        if 'xl/sharedStrings.xml' in z.namelist():
            root = ET.fromstring(z.read('xl/sharedStrings.xml'))
            shared = [''.join(t.text or '' for t in si.findall('.//m:t', ns)) for si in root.findall('m:si', ns)]
        wb = ET.fromstring(z.read('xl/workbook.xml'))
        sh = wb.find('m:sheets', ns)[0]
        rid = sh.attrib['{%s}id' % ns['r']]
        rels = ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        target = next(r.attrib['Target'] for r in rels.findall('p:Relationship', ns) if r.attrib['Id'] == rid)
        sp = 'xl/' + target.lstrip('/') if not target.startswith('xl/') else target
        root = ET.fromstring(z.read(sp))
        out = []
        for rr in root.findall('.//m:sheetData/m:row', ns):
            row = {}
            for cell in rr.findall('m:c', ns):
                i = col(cell.attrib.get('r'))
                typ = cell.attrib.get('t')
                v = cell.find('m:v', ns)
                raw = v.text if v is not None and v.text else ''
                if typ == 's' and raw:
                    val = shared[int(raw)]
                elif typ == 'inlineStr':
                    val = ''.join(cell.find('m:is', ns).itertext())
                else:
                    val = raw
                if i >= 0:
                    row[i] = val.strip()
            if row:
                out.append(row)
        return out


def parse_excel(path):
    if path.suffix.lower() not in {'.xlsx', '.xlsm'}:
        raise ValueError('فایل باید xlsx یا xlsm باشد.')
    rows = ooxml_rows(path)
    cols = {'barcode': 2, 'destination': 4, 'name': 7, 'address': 8}
    hp = None
    for i, r in enumerate(rows[:30]):
        n = {c: norm(v, True) for c, v in r.items()}
        b = [c for c, v in n.items() if 'بارکد' in v or 'رهگیری' in v]
        nm = [c for c, v in n.items() if 'نامگیرنده' in v or v in {'نامگ', 'گیرنده'}]
        if b and nm:
            hp = i
            cols['barcode'] = b[0]
            cols['name'] = nm[0]
            for c, v in n.items():
                if 'مقصد' in v:
                    cols['destination'] = c
                if 'آدرس' in v:
                    cols['address'] = c
            break
    out = []
    start = hp + 1 if hp is not None else 0
    for rn, r in enumerate(rows[start:], start=(hp + 2 if hp is not None else 1)):
        raw = str(r.get(cols['barcode'], '')).strip()
        code = re.sub(r'\D', '', raw.translate(DIGITS))
        if not code:
            continue
        if 'e+' in raw.lower() or len(code) < 10:
            raise ValueError(f'بارکد ردیف {rn} باید به صورت Text ذخیره شده باشد.')
        out.append({
            'row': rn,
            'code': code,
            'name': str(r.get(cols['name'], '')).strip(),
            'city': str(r.get(cols['destination'], '')).strip(),
            'address': str(r.get(cols['address'], '')).strip(),
        })
    if not out:
        raise ValueError('بارکد معتبری پیدا نشد.')
    return out


# ---------- Shopino authentication/API ----------
class AuthError(Exception):
    pass


def http_error(r, prefix='Shopino'):
    try:
        data = r.json()
        detail = data.get('detail') or data.get('message') or data.get('error') or str(data)
    except Exception:
        detail = r.text[:300]
    return RuntimeError(f'{prefix} HTTP {r.status_code}: {detail}')


def send_verification_code(phone):
    phone = normalize_phone(phone)
    if not re.fullmatch(r'09\d{9}', phone):
        raise ValueError('شماره موبایل معتبر نیست؛ مثل 09123456789 وارد کنید.')
    with httpx.Client(timeout=30, follow_redirects=True, headers=WEB_HEADERS) as client:
        r = client.post(SEND_CODE_URL, json={
            'phone': phone,
            'variant': 'web',
            'resend': True,
            'force_send': True,
        })
        if r.status_code >= 400:
            raise http_error(r, 'ارسال کد')
    return phone


def extract_sessionid(response, client):
    sid = response.cookies.get('sessionid') or client.cookies.get('sessionid')
    if sid:
        return sid
    for header in response.headers.get_list('set-cookie'):
        cookie = SimpleCookie()
        try:
            cookie.load(header)
            if 'sessionid' in cookie:
                return cookie['sessionid'].value
        except Exception:
            pass
    try:
        body = response.json()
        if isinstance(body, dict):
            for key in ('sessionid', 'session_id', 'session'):
                if body.get(key):
                    return str(body[key])
    except Exception:
        pass
    return None


def verify_phone(phone, code):
    phone = normalize_phone(phone)
    code = str(code or '').translate(DIGITS).strip()
    code = re.sub(r'\D', '', code)
    if not re.fullmatch(r'\d{4,8}', code):
        raise ValueError('کد تأیید معتبر نیست.')
    with httpx.Client(timeout=30, follow_redirects=True, headers=WEB_HEADERS) as client:
        r = client.post(VERIFY_PHONE_URL, json={
            'phone': phone,
            'code': code,
            'source': 'panel',
        })
        if r.status_code >= 400:
            raise http_error(r, 'تأیید کد')
        sid = extract_sessionid(r, client)
    if not sid:
        raise AuthError('ورود تأیید شد ولی sessionid در پاسخ شاپینو پیدا نشد؛ پاسخ Login نیاز به بررسی دارد.')
    count = Shopino(sid).probe()
    return sid, count


class Shopino:
    def __init__(self, sid):
        self.h = dict(WEB_HEADERS)
        self.h['cookie'] = f'sessionid={sid}'
        self.c = httpx.Client(timeout=35, follow_redirects=True, headers=self.h)

    def check(self, r):
        if r.status_code in (401, 403):
            raise AuthError('سشن شاپینو منقضی یا نامعتبر است.')
        if r.status_code >= 400:
            raise http_error(r)

    def probe(self):
        r = self.c.get(f'{BASE}/shops/{SHOP}/order-shippings/', params={'page': 1, 'page_size': 1})
        self.check(r)
        d = r.json()
        return d.get('count', '?')

    def list(self):
        url = f'{BASE}/shops/{SHOP}/order-shippings/'
        params = {'page': 1, 'page_size': 100}
        out = []
        seen = set()
        while url:
            r = self.c.get(url, params=params)
            self.check(r)
            d = r.json()
            params = None
            for x in d.get('results', []):
                if x.get('id') not in seen:
                    seen.add(x.get('id'))
                    out.append(x)
            url = d.get('next')
            if len(out) > 5000:
                break
        return out

    def one(self, i):
        r = self.c.get(f'{BASE}/shops/{SHOP}/order-shippings/{i}/')
        self.check(r)
        return r.json()

    def patch(self, i, code, typ='post'):
        r = self.c.patch(
            f'{BASE}/shops/{SHOP}/order-shippings/{i}/',
            json={'tracking_code': str(code), 'type': typ or 'post'},
        )
        self.check(r)
        return True


def api():
    sid = get('session')
    if not sid:
        raise AuthError('هنوز وارد شاپینو نشده‌اید. /login را بزنید.')
    return Shopino(sid)


def full_name(o):
    x = o.get('order') or {}
    return f"{x.get('first_name', '')} {x.get('last_name', '')}".strip()


def lev(a, b, limit=4):
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def candidates(row, orders):
    tn = norm(row['name'], True)
    tc = norm(row['city'], True)
    out = []
    for o in orders:
        cn = norm(full_name(o), True)
        city = str((o.get('order') or {}).get('city') or '')
        cc = norm(city, True)
        if not tn or not cn:
            continue
        d = lev(tn, cn)
        cm = bool(tc and cc and (tc == cc or tc in cc or cc in tc))
        ratio = SequenceMatcher(None, tn, cn).ratio()
        score = ratio * 70 + (25 if cm else 0) + (10 if d == 0 else 7 if d == 1 else 4 if d == 2 else 0)
        if d <= 2 or score >= 72:
            out.append({
                'id': int(o['id']),
                'name': full_name(o),
                'city': city,
                'type': o.get('type') or 'post',
                'tracking': str(o.get('tracking_code') or ''),
                'score': round(score, 1),
                'dist': d,
                'citymatch': cm,
            })
    out.sort(key=lambda x: (-x['score'], x['dist'], -x['id']))
    return out[:6]


def confident(cs):
    if not cs or not cs[0]['citymatch'] or cs[0]['dist'] > 2:
        return False
    if len(cs) == 1:
        return True
    return cs[0]['score'] - cs[1]['score'] >= 7 and not (cs[1]['citymatch'] and cs[1]['dist'] <= 2)


# ---------- Telegram ----------
async def access(update):
    u = update.effective_user
    if not get('owner'):
        setv('owner', u.id)
        add_user(u.id)
        await update.effective_chat.send_message(f'✅ شما مدیر اولیه شدید. Telegram ID: {u.id}')
        return True
    if allowed(u.id):
        return True
    await update.effective_chat.send_message(f'⛔️ دسترسی ندارید. ID شما: {u.id}\nمدیر: /allow {u.id}')
    return False


async def start(update, ctx):
    if not await access(update):
        return
    await update.message.reply_text(
        'ربات اتصال Vesta ↔ Shopino آماده است.\n\n'
        '1) برای ورود به شاپینو: /login\n'
        '2) شماره موبایل اکانت شاپینو را بفرستید.\n'
        '3) کد پیامک‌شده را همینجا بفرستید.\n'
        '4) بعد فایل xlsx/xlsm را ارسال کنید.\n\n'
        'موارد مطمئن خودکار ثبت می‌شوند و موارد مشکوک از شما سؤال می‌شوند.\n\n'
        '/status /allow /users\n'
        'ورود دستی با sessionid هم به‌عنوان حالت پشتیبان: /session'
    )


async def login_cmd(update, ctx):
    if not await access(update):
        return
    if get('owner') != str(update.effective_user.id):
        return await update.message.reply_text('فقط مدیر اولیه می‌تواند ورود شاپینو را انجام دهد.')
    clear_login_state(update.effective_user.id)
    set_login_state(update.effective_user.id, 'phone')
    await update.message.reply_text(
        '🔐 ورود شاپینو\n\nشماره موبایل اکانت شاپینو را بفرستید.\nمثال: 09123456789\n\n/cancel برای لغو'
    )


async def cancel_cmd(update, ctx):
    if not await access(update):
        return
    clear_login_state(update.effective_user.id)
    await update.message.reply_text('لغو شد.')


async def session(update, ctx):
    if not await access(update):
        return
    if get('owner') != str(update.effective_user.id):
        return await update.message.reply_text('فقط مدیر اولیه می‌تواند سشن را عوض کند.')
    if not ctx.args:
        return await update.message.reply_text('حالت پشتیبان: /session SESSION_ID\nبرای ورود عادی از /login استفاده کنید.')
    text = ' '.join(ctx.args)
    m = re.search(r'sessionid=([^;\s]+)', text)
    sid = m.group(1) if m else text.strip().split()[0]
    try:
        count = await asyncio.to_thread(Shopino(sid).probe)
        setv('session', sid)
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.effective_chat.send_message(f'✅ اتصال شاپینو برقرار شد؛ {count} سفارش قابل مشاهده است.')
    except Exception as e:
        await update.message.reply_text(f'❌ سشن ذخیره نشد: {e}')


async def status(update, ctx):
    if not await access(update):
        return
    try:
        count = await asyncio.to_thread(api().probe)
        await update.message.reply_text(f'✅ Shopino API: OK | سفارش‌ها: {count}')
    except Exception as e:
        await update.message.reply_text(f'❌ {e}\nبرای ورود /login را بزنید.')


async def allow_cmd(update, ctx):
    if not await access(update):
        return
    if get('owner') != str(update.effective_user.id):
        return await update.message.reply_text('فقط مدیر می‌تواند دسترسی بدهد.')
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text('/allow TELEGRAM_ID')
    add_user(int(ctx.args[0]))
    await update.message.reply_text('✅ اضافه شد.')


async def users(update, ctx):
    if not await access(update):
        return
    with db() as c:
        ids = [str(r[0]) for r in c.execute('SELECT id FROM users')]
    await update.message.reply_text('Owner: ' + str(get('owner')) + '\nUsers: ' + ', '.join(ids))


def keyboard(t, cs):
    b = []
    for c in cs[:5]:
        label = f"{c['id']} | {c['name']} | {c['city']}"
        b.append([InlineKeyboardButton(label[:60], callback_data=f'p:{t}:{c["id"]}')])
    b.append([
        InlineKeyboardButton('🔢 ID دستی', callback_data=f'm:{t}'),
        InlineKeyboardButton('⏭ رد', callback_data=f's:{t}'),
    ])
    return InlineKeyboardMarkup(b)


async def ask(chat, t, p):
    r = p['row']
    await chat.send_message(
        f"⚠️ نیاز به تأیید\nردیف {r['row']} | {r['name']} | {r['city']}\n"
        f"کد: {r['code']}\n\nکدام سفارش شاپینو است؟",
        reply_markup=keyboard(t, p['candidates']),
    )


async def document(update, ctx):
    if not await access(update):
        return
    try:
        client = api()
    except Exception as e:
        return await update.message.reply_text(f'⚠️ {e}\n/login')

    d = update.message.document
    suffix = Path(d.file_name or '').suffix.lower()
    if suffix not in {'.xlsx', '.xlsm'}:
        return await update.message.reply_text('فقط xlsx/xlsm بفرستید.')

    msg = await update.message.reply_text('📥 در حال خواندن فایل…')
    tmp = Path(tempfile.gettempdir()) / f'{uuid.uuid4().hex}{suffix}'
    try:
        f = await d.get_file()
        await f.download_to_drive(custom_path=str(tmp))
        rows = await asyncio.to_thread(parse_excel, tmp)
        await msg.edit_text(f'📄 {len(rows)} بارکد؛ در حال دریافت سفارش‌های شاپینو…')
        orders = await asyncio.to_thread(client.list)

        ok = old = review = fail = 0
        pend = []
        for i, r in enumerate(rows, 1):
            cs = candidates(r, orders)
            if confident(cs):
                c = cs[0]
                if c['tracking'] == r['code']:
                    old += 1
                elif c['tracking']:
                    review += 1
                    pend.append({'row': r, 'candidates': cs})
                else:
                    try:
                        await asyncio.to_thread(client.patch, c['id'], r['code'], c['type'])
                        ok += 1
                        c['tracking'] = r['code']
                    except AuthError:
                        raise
                    except Exception:
                        fail += 1
            else:
                review += 1
                pend.append({'row': r, 'candidates': cs})
            if i % 10 == 0:
                await msg.edit_text(f'⏳ {i}/{len(rows)} | ✅ {ok} | ⚠️ {review}')

        await msg.edit_text(
            f'✅ تمام شد\nثبت خودکار: {ok}\nاز قبل ثبت: {old}\nنیاز به تأیید: {review}\nخطا: {fail}'
        )
        for p in pend:
            t = save_pending(update.effective_chat.id, update.effective_user.id, p)
            await ask(update.effective_chat, t, p)
    except AuthError:
        await msg.edit_text('🔐 نشست شاپینو منقضی شده. /login را بزنید و دوباره فایل را بفرستید.')
    except Exception as e:
        log.exception('import failed')
        await msg.edit_text(f'❌ خطا: {e}')
    finally:
        tmp.unlink(missing_ok=True)


async def choose(q, t, i):
    p = pending(t)
    if not p or p['status'] != 'pending':
        return await q.edit_message_text('این مورد قبلاً بررسی شده.')
    r = p['payload']['row']
    cs = p['payload']['candidates']
    c = next((x for x in cs if x['id'] == i), None)
    client = api()
    if not c:
        o = await asyncio.to_thread(client.one, i)
        c = {
            'id': i,
            'name': full_name(o),
            'city': str((o.get('order') or {}).get('city') or ''),
            'type': o.get('type') or 'post',
            'tracking': str(o.get('tracking_code') or ''),
        }
    if c['tracking'] and c['tracking'] != r['code']:
        return await q.edit_message_text(f"⚠️ سفارش {i} کد دیگری دارد ({c['tracking']})؛ overwrite نکردم.")
    if c['tracking'] != r['code']:
        await asyncio.to_thread(client.patch, i, r['code'], c['type'])
    done(t)
    await q.edit_message_text(f"✅ ثبت شد: {r['name']} → سفارش {i}\n{r['code']}")


async def callback(update, ctx):
    q = update.callback_query
    if not allowed(q.from_user.id):
        return await q.answer('دسترسی ندارید', show_alert=True)
    await q.answer()
    parts = (q.data or '').split(':')
    a, t = parts[0], parts[1]
    p = pending(t)
    if not p or p['status'] != 'pending':
        return await q.edit_message_text('این مورد قبلاً بررسی شده.')
    try:
        if a == 's':
            done(t, 'skipped')
            return await q.edit_message_text('⏭ رد شد؛ تغییری در شاپینو انجام نشد.')
        if a == 'm':
            with db() as c:
                c.execute(
                    'INSERT INTO manual VALUES(?,?) ON CONFLICT(user) DO UPDATE SET token=excluded.token',
                    (q.from_user.id, t),
                )
            return await q.edit_message_text('🔢 ID عددی order-shipping شاپینو را بفرستید.')
        if a in {'p', 'c'}:
            return await choose(q, t, int(parts[2]))
    except AuthError:
        await q.edit_message_text('🔐 نشست شاپینو منقضی شده. /login را بزنید.')
    except Exception as e:
        await q.edit_message_text(f'❌ خطا: {e}')


async def text(update, ctx):
    if not await access(update):
        return
    uid = update.effective_user.id
    text_value = (update.message.text or '').strip()

    # Interactive Shopino login has priority over manual order matching.
    state = login_state(uid)
    if state:
        if get('owner') != str(uid):
            clear_login_state(uid)
            return await update.message.reply_text('فقط مدیر اولیه می‌تواند ورود شاپینو را انجام دهد.')

        if state['step'] == 'phone':
            phone = normalize_phone(text_value)
            try:
                phone = await asyncio.to_thread(send_verification_code, phone)
                set_login_state(uid, 'code', phone)
                try:
                    await update.message.delete()
                except Exception:
                    pass
                return await update.effective_chat.send_message(
                    f'📲 کد تأیید شاپینو به شماره ••••{phone[-4:]} ارسال شد.\nکد را همینجا بفرستید.\n\n/cancel برای لغو'
                )
            except Exception as e:
                return await update.message.reply_text(f'❌ ارسال کد انجام نشد: {e}\nشماره را دوباره بفرستید.')

        if state['step'] == 'code':
            code = text_value.translate(DIGITS)
            try:
                sid, count = await asyncio.to_thread(verify_phone, state['phone'], code)
                setv('session', sid)
                clear_login_state(uid)
                try:
                    await update.message.delete()
                except Exception:
                    pass
                return await update.effective_chat.send_message(
                    f'✅ ورود شاپینو موفق بود.\nاتصال API برقرار است و {count} سفارش قابل مشاهده است.\n\nحالا فایل اکسل را بفرستید.'
                )
            except Exception as e:
                return await update.message.reply_text(f'❌ ورود ناموفق: {e}\nکد را دوباره بفرستید یا /login را از اول بزنید.')

    with db() as c:
        r = c.execute('SELECT token FROM manual WHERE user=?', (uid,)).fetchone()
    if not r:
        return
    t = r['token']
    s = text_value.translate(DIGITS)
    if not s.isdigit():
        return await update.message.reply_text('فقط ID عددی را بفرستید.')
    try:
        o = await asyncio.to_thread(api().one, int(s))
        name = full_name(o)
        city = str((o.get('order') or {}).get('city') or '')
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton('✅ تأیید و ثبت', callback_data=f'c:{t}:{s}'),
            InlineKeyboardButton('لغو', callback_data=f's:{t}'),
        ]])
        await update.message.reply_text(
            f'سفارش پیدا شد: {s} | {name} | {city}\nثبت روی همین سفارش؟',
            reply_markup=kb,
        )
    except AuthError:
        await update.message.reply_text('🔐 نشست شاپینو منقضی شده. /login را بزنید.')
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({
            'status': 'ok',
            'service': 'ordersvesta',
            'shop_id': SHOP,
            'shopino_session_configured': bool(get('session')),
        }).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def main():
    s = ThreadingHTTPServer(('0.0.0.0', 8080), Health)
    threading.Thread(target=s.serve_forever, daemon=True).start()

    a = Application.builder().token(TOKEN).build()
    a.add_handler(CommandHandler('start', start))
    a.add_handler(CommandHandler('help', start))
    a.add_handler(CommandHandler('login', login_cmd))
    a.add_handler(CommandHandler('cancel', cancel_cmd))
    a.add_handler(CommandHandler('session', session))
    a.add_handler(CommandHandler('status', status))
    a.add_handler(CommandHandler('allow', allow_cmd))
    a.add_handler(CommandHandler('users', users))
    a.add_handler(CallbackQueryHandler(callback))
    a.add_handler(MessageHandler(filters.Document.ALL, document))
    a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))
    a.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
