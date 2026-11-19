"""Separate Liora -> Mahak accounting product wizard.

A successful SaveAllData response is only cloud acceptance. It does NOT prove
Mahak desktop imported the product. Stable request IDs prevent duplicate sends.
This flow never publishes WooCommerce products.
"""
import asyncio
import json
import re
import secrets
import tempfile
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import main as m
from app import operations as ops
from app.bridge_client import BridgeWooClient

LABEL = '➕ ثبت محصول جدید'
PERSIAN_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
CODE_RE = re.compile(r'^[A-Za-z0-9_-]{1,24}$')


def _normalize(value):
    return (value or '').translate(PERSIAN_DIGITS).replace(',', '').replace('٬', '').strip()


def _confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ ارسال به API محک', callback_data='mahak:submit')],
        [InlineKeyboardButton('❌ انصراف', callback_data='mahak:cancel')],
    ])


def _status_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🔎 وضعیت محک و سایت', callback_data='mahak:status')],
        [InlineKeyboardButton('➕ کالای بعدی', callback_data='mahak:new')],
    ])


def _photos_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ عکس‌ها تمام شد', callback_data='mahak:photos_done')],
        [InlineKeyboardButton('⏭ بدون عکس', callback_data='mahak:photos_done')],
        [InlineKeyboardButton('❌ انصراف', callback_data='mahak:cancel')],
    ])


def _skip_keyboard(what):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('⏭ بدون تغییر / رد کردن', callback_data=f'mahak:{what}_skip')],
        [InlineKeyboardButton('❌ انصراف', callback_data='mahak:cancel')],
    ])


_PICTURE_LOCKS = {}



with m.db() as conn:
    conn.execute('''CREATE TABLE IF NOT EXISTS mahak_product_submissions (
        request_key TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.execute('''CREATE INDEX IF NOT EXISTS idx_mahak_user_created
        ON mahak_product_submissions(user_id, created_at DESC)''')
    columns = [x[1] for x in conn.execute('PRAGMA table_info(mahak_product_submissions)').fetchall()]
    if 'data_json' not in columns:
        conn.execute('ALTER TABLE mahak_product_submissions ADD COLUMN data_json TEXT')


def _save_job(uid, data, status):
    with m.db() as conn:
        conn.execute('''INSERT INTO mahak_product_submissions(request_key,user_id,code,name,status,data_json)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(request_key) DO UPDATE SET status=excluded.status,data_json=excluded.data_json''', (
            data['request_key'], uid, data['code'], data['name'], status,
            json.dumps(data, ensure_ascii=False),
        ))


def _last_job(uid):
    with m.db() as conn:
        row = conn.execute('''SELECT request_key,code,name,status,data_json
            FROM mahak_product_submissions WHERE user_id=?
            ORDER BY created_at DESC, rowid DESC LIMIT 1''', (uid,)).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        parsed = json.loads(data.get('data_json') or '{}')
        if isinstance(parsed, dict):
            parsed.update({k: data[k] for k in ('request_key','code','name','status')})
            return parsed
    except (ValueError, TypeError):
        pass
    return data


def _status_label(result):
    status = str(result.get('status') or 'unknown')
    if status == 'visible_in_cloud_export':
        return '✅ کالا در فهرست خروجی ابری محک دیده می‌شود. وجود آن در نرم‌افزار ویندوزی باید جداگانه کنترل شود.'
    if status == 'accepted_pending_sync':
        return '🟠 API محک درخواست را قبول کرده؛ هنوز تأیید ورود به نرم‌افزار ویندوزی نداریم.'
    if status == 'unknown':
        return '⚠️ نتیجه نامشخص است. ثبت دوباره با درخواست جدید ممنوع است؛ فقط وضعیت را بررسی کنید.'
    if status == 'rejected':
        return '❌ درخواست ثبت توسط API رد شده است.'
    if status == 'submitting':
        return '⏳ ارسال هنوز در جریان است یا پاسخ قطعی دریافت نشده. درخواست تازه نفرستید.'
    return '⚠️ هیچ درخواست ثبت معتبری در سمت اتصال پیدا نشد.'


async def start(update, ctx=None):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    ops.set_state(uid, 'mahak_product', 'name', {})
    await update.effective_chat.send_message(
        '➕ ثبت محصول جدید — ربات ← محک ← سایت\n\n'
        'فقط یک بار محصول را تعریف می‌کنی. اول در API محک ثبت می‌شود؛ '
        'بعد از همگام‌سازی بازارا، پیش‌نویس سایت خودش ساخته می‌شود و '
        'عکس، کپشن و وزن هم روی همان کالا قرار می‌گیرند.\n\n'
        'فعلاً ثبت حسابداری برای *کالای ساده با موجودی اولیه صفر* تأیید شده است. '
        'محصول دارای تنوع یا موجودی اولیه نیازمند تأیید جداگانه حسابداری است.\n\n'
        'نام کالا را بفرست.',
        parse_mode='Markdown',
        reply_markup=ops.cancel_menu(),
    )


async def command_status(update, ctx=None):
    if not await m.access(update):
        return
    await check_status(update.effective_user.id, update.effective_chat)


async def check_status(uid, chat):
    state = ops.get_state(uid)
    data = state['data'] if state and state['flow'] == 'mahak_product' else {}
    if not data.get('request_key'):
        data = _last_job(uid) or {}
    if not data.get('request_key'):
        await chat.send_message('هنوز درخواست کالای محک برای بررسی ثبت نشده است.')
        return
    try:
        result = await asyncio.to_thread(
            BridgeWooClient().call, 'mahak_status',
            {'request_key': data['request_key']},
        )
    except Exception as exc:
        await chat.send_message(f'❌ بررسی وضعیت ناموفق بود: {exc}')
        return
    _save_job(uid, data, str(result.get('status') or 'unknown'))
    if result.get('status') == 'not_submitted':
        state = ops.get_state(uid)
        if (state and state.get('flow') == 'mahak_product'
                and state['data'].get('request_key') == data['request_key']
                and state.get('step') == 'submitted'):
            # WordPress has no idempotency record: original request never began.
            # Reuse the SAME request key instead of creating a fresh product.
            ops.update_state(uid, step='preview', data=state['data'])
            await chat.send_message(
                'این شناسه در سرور ثبت نشده است. می‌توانید همان درخواست قبلی را '
                'بدون تغییر کد کالا دوباره تأیید کنید.',
                reply_markup=_confirm_keyboard(),
            )
            return
    await chat.send_message(
        f"{_status_label(result)}\n\nکد کالا: {data.get('code', '—')}"
        f"\n{result.get('note') or ''}",
        reply_markup=_status_keyboard(),
    )


async def maybe_text(update, ctx):
    text = (update.message.text or '').strip()
    if text in {LABEL, '🧾 ثبت کالا در محک', '🧾 ساخت کالای جدید در محک'}:
        await start(update, ctx)
        return True

    uid = update.effective_user.id
    state = ops.get_state(uid)
    if not state or state.get('flow') != 'mahak_product':
        return False

    # Standard top-level buttons keep their existing route and discard draft.
    if text in {'⬅️ منوی اصلی', '❌ لغو عملیات',
                '🛍 مدیریت ووکامرس', '📦 رهگیری شاپینو',
                '⚙️ تنظیمات و اتصال‌ها'}:
        return False
    if not await m.access(update):
        return True
    data = state['data']
    step = state['step']
    chat = update.effective_chat
    if step == 'name':
        if len(text) < 3 or len(text) > 180:
            await chat.send_message('نام کالا باید حداقل ۳ و حداکثر ۱۸۰ نویسه داشته باشد.')
            return True
        data['name'] = text
        ops.update_state(uid, step='code', data=data)
        await chat.send_message('کد یکتای کالای جدید در محک را بفرستید (مثلاً 998813).')
        return True

    if step == 'code':
        code = _normalize(text)
        if not CODE_RE.fullmatch(code):
            await chat.send_message('کد باید ۱ تا ۲۴ نویسه و شامل عدد یا حروف انگلیسی باشد.')
            return True
        data['code'] = code
        ops.update_state(uid, step='template', data=data)
        await chat.send_message(
            'کد یک کالای مشابه که *از قبل در محک وجود دارد* را بفرستید.\n'
            'دسته‌بندی و واحد شمارش از آن کالا گرفته می‌شود و خود آن تغییر نمی‌کند.',
            parse_mode='Markdown',
        )
        return True

    if step == 'template':
        code = _normalize(text)
        if not CODE_RE.fullmatch(code) or code == data['code']:
            await chat.send_message('کد الگو نامعتبر است یا با کد کالای جدید یکسان است.')
            return True
        try:
            template = await asyncio.to_thread(
                BridgeWooClient().call, 'mahak_template', {'template_code': code},
            )
        except Exception as exc:
            await chat.send_message(f'کالای الگو در محک تأیید نشد: {exc}\nکد معتبر دیگری بفرستید.')
            return True
        data['template_code'] = code
        data['template_name'] = template.get('name', '')
        data['category_id'] = template.get('category_id')
        data['unit_name'] = template.get('unit_name')
        ops.update_state(uid, step='price', data=data)
        await chat.send_message(
            f"الگو: {data['template_name']}\n"
            f"شناسه دسته‌بندی: {data['category_id']}\n"
            f"واحد: {data['unit_name']}\n\n"
            "قیمت فروش کالای جدید را به **ریال** وارد کنید (فقط عدد).\n"
            "موجودی اولیه برای جلوگیری از ایجاد سند انبار، صفر خواهد بود.",
            parse_mode='Markdown',
        )
        return True

    if step == 'price':
        price = _normalize(text)
        if not re.fullmatch(r'\d{1,14}', price) or int(price) <= 0:
            await chat.send_message('قیمت ریالی باید یک عدد صحیح مثبت باشد.')
            return True
        data['price_rial'] = price
        data['request_key'] = secrets.token_hex(16)
        data['image_ids'] = []
        data['caption'] = None
        data['weight_grams'] = None
        ops.update_state(uid, step='photos', data=data)
        await chat.send_message(
            '📸 حالا عکس‌های محصول را بفرست (حداکثر ۱۰ عکس).\n'
            'عکس اول کاور خواهد شد، بقیه گالری. اگر تصویری نداری «بدون عکس» را بزن.\n\n'
            'این عکس‌ها فعلاً در کتابخانه رسانه وردپرس نگهداری می‌شوند؛ '
            'پس از تأیید ثبت محک، به پیش‌نویس همان محصول متصل می‌شوند.',
            reply_markup=_photos_keyboard(),
        )
        return True

    if step == 'caption':
        if len(text)>4000 or not text.strip():
            await chat.send_message('کپشن باید بین ۱ تا ۴۰۰۰ کاراکتر باشد.')
            return True
        data['caption']=text.strip()
        ops.update_state(uid,step='weight',data=data)
        await chat.send_message(
            '⚖️ وزن بسته‌بندی محصول را به گرم بفرست؛ مثلاً ۱۲۰.\n'
            'اگر وزن مشخص نیست، «رد کردن» را بزن.',
            reply_markup=_skip_keyboard('weight'),
        )
        return True

    if step == 'weight':
        weight=_normalize(text).replace('٫','.')
        if not re.fullmatch(r'\d{1,7}(?:\.\d{1,3})?',weight) or not 0 < float(weight) <= 1000000:
            await chat.send_message('وزن را عدد مثبت به گرم بفرست، مثل ۱۵۰.')
            return True
        data['weight_grams']=weight
        return await _preview(uid,chat,data)

    await chat.send_message('برای ادامه از دکمه‌های همین مرحله استفاده کنید.')
    return True



async def maybe_photo(update, ctx):
    if not update.message or not update.message.photo:
        return False
    uid=update.effective_user.id
    state=ops.get_state(uid)
    if not state or state.get('flow')!='mahak_product':
        return False
    if not await m.access(update):
        return True
    lock=_PICTURE_LOCKS.setdefault(uid,asyncio.Lock())
    async with lock:
        state=ops.get_state(uid)
        if not state or state.get('step')!='photos':
            await update.message.reply_text('دریافت عکس این کالا تمام شده است.')
            return True
        data=state['data']
        if len(data.get('image_ids') or [])>=10:
            await update.message.reply_text('حداکثر ۱۰ عکس برای هر کالا مجاز است.')
            return True
        filename='mahak_new_'+uuid.uuid4().hex+'.jpg'
        temp=Path(tempfile.gettempdir())/filename
        try:
            file=await update.message.photo[-1].get_file(read_timeout=60,connect_timeout=30)
            await file.download_to_drive(custom_path=str(temp),read_timeout=120,connect_timeout=30)
            media=await asyncio.to_thread(BridgeWooClient().upload_media,temp,filename)
            aid=int(media.get('id') or 0)
            if aid<=0:raise RuntimeError('رسانه سایت شناسه معتبر برنگرداند.')
            data.setdefault('image_ids',[]).append(aid)
            ops.update_state(uid,data=data)
            await update.message.reply_text(
                f"✅ عکس {len(data['image_ids'])} دریافت شد. عکس بعدی را بفرست یا «عکس‌ها تمام شد» را بزن.",
                reply_markup=_photos_keyboard(),
            )
        except Exception as exc:
            await update.message.reply_text(f'❌ آپلود عکس ناموفق بود: {exc}')
        finally:
            temp.unlink(missing_ok=True)
    return True


async def _preview(uid,chat,data):
    ops.update_state(uid,step='preview',data=data)
    caption=data.get('caption')
    weight=data.get('weight_grams')
    await chat.send_message(
        '🧾 پیش‌نمایش ثبت در محک و آماده‌سازی سایت\n\n'
        f"نام: {data['name']}\nکد محک: {data['code']}\n"
        f"الگوی دسته/واحد: {data['template_name']}\n"
        f"قیمت محک: {int(data['price_rial']):,} ریال\n"
        f"نوع: ساده | موجودی اولیه: ۰\n"
        f"عکس‌ها: {len(data.get('image_ids') or [])}\n"
        f"کپشن: {str(caption)[:330] if caption else 'بدون تغییر'}\n"
        f"وزن: {str(weight)+' گرم' if weight is not None else 'نامشخص'}\n\n"
        'ابتدا فقط یک درخواست ثبت در محک ارسال می‌شود. '
        'پس از تأیید و همگام‌سازی، اطلاعات سایت به همان کالا متصل می‌شوند؛ '
        'هیچ محصولی خودکار منتشر نمی‌شود.',
        reply_markup=_confirm_keyboard(),
    )
    return True


async def _stage_for_site(uid,data,chat):
    payload={
        'request_key':data['request_key'],
        'code':data['code'],
        'image_ids':data.get('image_ids') or [],
    }
    if data.get('caption') is not None:
        payload['caption']=data['caption']
    if data.get('weight_grams') is not None:
        payload['weight_grams']=data['weight_grams']
    try:
        queued=await asyncio.to_thread(
            BridgeWooClient().call,'mahak_content_stage',payload,
        )
        data['content_staged']=True
        ops.update_state(uid,data=data)
        _save_job(uid,data,str(data.get('mahak_status') or 'accepted_pending_sync'))
        await chat.send_message(
            '📤 محتوای محصول برای اتصال خودکار به پیش‌نویس سایت ثبت شد.\n'
            f"وضعیت: {queued.get('status','queued')}\n"
            'منتظر همگام‌سازی و تأیید شناسه محک می‌ماند؛ '
            'عکس/کپشن/وزن را لازم نیست در مسیر دیگری وارد کنی.'
        )
    except Exception as exc:
        data['content_staged']=False
        ops.update_state(uid,data=data)
        _save_job(uid,data,str(data.get('mahak_status') or 'accepted_pending_sync'))
        await chat.send_message(
            f'⚠️ محک درخواست ثبت را پذیرفته اما صف محتوای سایت تأیید نشد: {exc}\n'
            'درخواست تازه برای همان کالا نفرست؛ از «وضعیت محک و سایت» '
            'دوباره همین درخواست را پیگیری کن.',
            reply_markup=_status_keyboard(),
        )


async def maybe_callback(update, ctx):
    q = update.callback_query
    action = q.data or ''
    if not action.startswith('mahak:'):
        return False
    if not m.allowed(q.from_user.id):
        await q.answer('شما دسترسی ندارید.', show_alert=True)
        return True
    await q.answer()
    uid = q.from_user.id
    chat = q.message.chat
    if action == 'mahak:cancel':
        state = ops.get_state(uid)
        if state and state['flow'] == 'mahak_product':
            ops.clear_state(uid)
        await chat.send_message('عملیات لغو شد.', reply_markup=ops.main_menu())
        return True
    if action == 'mahak:new':
        ops.set_state(uid, 'mahak_product', 'name', {})
        await chat.send_message('نام کالای جدید را بفرستید.', reply_markup=ops.cancel_menu())
        return True
    if action == 'mahak:status':
        await check_status(uid, chat)
        return True
    state=ops.get_state(uid)
    if state and state.get('flow')=='mahak_product':
        data=state['data']
        if action=='mahak:photos_done' and state['step']=='photos':
            lock=_PICTURE_LOCKS.setdefault(uid,asyncio.Lock())
            async with lock:
                state=ops.get_state(uid)
                if not state or state['step']!='photos':return True
                data=state['data']
                ops.update_state(uid,step='caption',data=data)
                await chat.send_message(
                    '✍️ توضیح کوتاه / کپشن محصول را بفرست.\n'
                    'اگر نمی‌خواهی کپشنی ثبت شود، «رد کردن» را بزن.',
                    reply_markup=_skip_keyboard('caption'),
                )
            return True
        if action=='mahak:caption_skip' and state['step']=='caption':
            data['caption']=None
            ops.update_state(uid,step='weight',data=data)
            await chat.send_message('⚖️ وزن بسته‌بندی را به گرم بفرست؛ یا «رد کردن» را بزن.',
                                    reply_markup=_skip_keyboard('weight'))
            return True
        if action=='mahak:weight_skip' and state['step']=='weight':
            data['weight_grams']=None
            return await _preview(uid,chat,data)
    if action != 'mahak:submit':
        return True

    state = ops.get_state(uid)
    if not state or state['flow'] != 'mahak_product' or state['step'] not in ('preview', 'submitted'):
        await chat.send_message('پیش‌نمایش منقضی شده؛ دوباره از منو شروع کنید.')
        return True
    data = state['data']
    if state['step'] == 'submitted':
        await check_status(uid, chat)
        return True

    ops.update_state(uid, step='submitted', data=data)
    _save_job(uid, data, 'submitting')
    await chat.send_message('در حال ارسال یک‌باره به API محک و ثبت شناسه پیگیری…')
    try:
        result = await asyncio.to_thread(
            BridgeWooClient().call, 'mahak_submit',
            {k: data[k] for k in ('request_key', 'name', 'code', 'template_code', 'price_rial')},
        )
    except Exception as exc:
        # Do not create a new request_key; the same operation must be reconciled.
        await chat.send_message(
            f'⚠️ پاسخ قطعی از API دریافت نشد: {exc}\n'
            'برای جلوگیری از ثبت تکراری فقط «بررسی همگام‌سازی» را بزنید.',
            reply_markup=_status_keyboard(),
        )
        return True
    data['mahak_status']=str(result.get('status') or 'unknown')
    _save_job(uid, data, data['mahak_status'])
    await chat.send_message(
        f"{_status_label(result)}\n\n"
        f"کد: {data['code']}\n{result.get('note') or ''}",
        reply_markup=_status_keyboard(),
    )
    if data['mahak_status'] in ('accepted_pending_sync','visible_in_cloud_export'):
        await _stage_for_site(uid,data,chat)
    return True
