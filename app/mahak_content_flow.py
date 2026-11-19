"""Complete photos, short caption and shipping weight on existing Mahak-linked Woo products.

The accountant owns ERP names, variants, price and stock. This workflow only
attaches images and editorial details to a verified existing Woo parent.
"""
import asyncio
import re
import secrets
import tempfile
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import main as m
from app import operations as ops
from app.bridge_client import BridgeWooClient

LABEL = '📸 عکس و محتوای کالای محک'
FLOW = 'mahak_content'
NAVIGATION = {
    '❌ لغو عملیات', '⬅️ منوی اصلی', '🛍 مدیریت ووکامرس',
    '📦 رهگیری شاپینو', '⚙️ تنظیمات و اتصال‌ها',
    '🧾 ثبت کالا در محک', '🧾 ساخت کالای جدید در محک',
    '📥 کالای محک به سایت', '➕ ثبت محصول جدید',
}
_PICTURE_LOCKS = {}
_PERSIAN_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')


def _keyboard(*rows):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text, callback_data=action) for text, action in row]
        for row in rows
    ])


def _search_keyboard(items):
    rows = [
        [(f"{'✅' if p.get('woo_id') else '⏳'} {i+1}. {str(p.get('name', ''))[:45]}",
          f'mahakcontent:pick:{i}')]
        for i, p in enumerate(items)
    ]
    rows += [[('🔍 جست‌وجوی دوباره', 'mahakcontent:again')],
             [('❌ لغو', 'mahakcontent:cancel')]]
    return _keyboard(*rows)


def _photo_keyboard():
    return _keyboard(
        [('✅ تصاویر تمام شد', 'mahakcontent:photos_done')],
        [('⏭ بدون ارسال عکس', 'mahakcontent:photos_done')],
        [('❌ انصراف', 'mahakcontent:cancel')],
    )


def _cover_keyboard():
    return _keyboard(
        [('🖼 جایگزینی عکس اصلی', 'mahakcontent:cover:replace')],
        [('🗂 نگه‌داشتن عکس اصلی؛ تصاویر جدید به گالری', 'mahakcontent:cover:keep')],
        [('❌ انصراف', 'mahakcontent:cancel')],
    )


def _skip_keyboard(section):
    return _keyboard(
        [('⏭ حفظ مقدار فعلی', f'mahakcontent:{section}_skip')],
        [('❌ انصراف', 'mahakcontent:cancel')],
    )


def _confirm_keyboard():
    return _keyboard(
        [('✅ ذخیره محتوای سایت', 'mahakcontent:save')],
        [('❌ انصراف', 'mahakcontent:cancel')],
    )


def _retry_keyboard():
    return _keyboard(
        [('🔄 بررسی/ارسال مجدد همین درخواست', 'mahakcontent:retry')],
        [('❌ انصراف', 'mahakcontent:cancel')],
    )


async def start(update, ctx=None):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    ops.set_state(uid, FLOW, 'search', {})
    await update.effective_chat.send_message(
        '📸 تکمیل محتوای محصول ثبت‌شده در محک\n\n'
        'نام یا کد کالای محک را بنویس. اگر محصول در سایت موجود باشد، '
        'عکس اصلی، گالری، کپشن و وزن بسته‌بندی را روی همان محصول تکمیل می‌کنیم.\n\n'
        'نام حسابداری، قیمت، موجودی و تنوع‌ها از ربات تغییر نمی‌کنند. '
        'اگر کالا تازه است، اول باید در محک ثبت و از بازارا همگام شده باشد.',
        reply_markup=ops.cancel_menu(),
    )


async def _search(uid, chat, query):
    if len(query) < 2 or len(query) > 100:
        await chat.send_message('نام یا کد کالا باید بین ۲ تا ۱۰۰ نویسه باشد.')
        return
    sent = await chat.send_message('🔎 بررسی محصول موجود در محک و سایت…')
    try:
        result = await asyncio.to_thread(
            BridgeWooClient().call, 'mahak_site_search', {'query': query},
        )
        items = (result.get('items') or [])[:8]
        if not isinstance(items, list):
            items = []
    except Exception as exc:
        await sent.edit_text(f'❌ جست‌وجوی محک ناموفق بود: {exc}')
        return
    if not items:
        await sent.edit_text(
            'کالایی پیدا نشد. اگر محصول تازه ثبت شده، ابتدا F2 بازارا را انجام بده '
            'و بعد نام کوتاه‌تر یا کد محصول را امتحان کن.'
        )
        return
    ops.update_state(uid, step='select', data={'items': items})
    await sent.edit_text(
        'کالای درست را انتخاب کن. ✅ یعنی نسخه آن در سایت هست؛ '
        '⏳ یعنی هنوز باید همگام‌سازی شود.',
        reply_markup=_search_keyboard(items),
    )


async def _begin_selected(uid, chat, item):
    if not item.get('woo_id'):
        await chat.send_message(
            'این کالا هنوز در سایت شناسه تأییدشده ندارد. نسخه دوم نمی‌سازم. '
            'بعد از F2، سیستم خودکار پیش‌نویس را می‌سازد؛ سپس همین‌جا تکمیلش کن.'
        )
        return
    selected = {k: item[k] for k in ('product_id', 'code', 'name')}
    try:
        info = await asyncio.to_thread(
            BridgeWooClient().call, 'mahak_content_status',
            {'product_id': selected['product_id'], 'code': selected['code'],
             'woo_id': int(item['woo_id'])},
        )
    except Exception as exc:
        await chat.send_message(f'❌ شناسه محصول محک و سایت تأیید نشد: {exc}')
        return
    data = {
        'selected': selected,
        'woo_id': int(info['woo_id']),
        'original': info,
        'image_ids': [],
        'caption': None,
        'weight_grams': None,
        'cover_mode': 'if_empty',
    }
    ops.set_state(uid, FLOW, 'photos', data)
    existing = 'دارد' if info.get('image_id') else 'ندارد'
    await chat.send_message(
        f"✅ محصول تأیید شد: {info.get('name')}\n"
        f"شناسه سایت: {info['woo_id']} | وضعیت: {info.get('status')}\n"
        f"عکس اصلی فعلی: {existing} | گالری: {len(info.get('gallery_image_ids') or [])}\n\n"
        '📷 حالا عکس‌های محصول را بفرست. می‌توانی چند عکس پشت سر هم بفرستی؛ '
        'اولی تصویر اصلی پیشنهادی و بقیه گالری خواهند بود.\n'
        'وقتی تمام شد «تصاویر تمام شد» را بزن. اگر عکس لازم نداری همان دکمه را بزن.',
        reply_markup=_photo_keyboard(),
    )


async def maybe_text(update, ctx):
    if not update.message or not update.message.text:
        return False
    value = update.message.text.strip()
    if value == LABEL:
        await start(update, ctx)
        return True
    uid = update.effective_user.id
    state = ops.get_state(uid)
    if not state or state.get('flow') != FLOW or value in NAVIGATION:
        return False
    if not await m.access(update):
        return True
    step = state['step']
    chat = update.effective_chat
    if step == 'search':
        await _search(uid, chat, value)
    elif step == 'caption':
        if len(value) > 4000:
            await chat.send_message('کپشن حداکثر ۴۰۰۰ نویسه می‌تواند باشد.')
            return True
        data = state['data']
        data['caption'] = value
        ops.update_state(uid, step='weight', data=data)
        await chat.send_message(
            '⚖️ وزن بسته‌بندی را به گرم بفرست (مثلاً ۱۵۰ یا ۲۵۰). '
            'ربات خودش به واحد تنظیم‌شده ووکامرس تبدیل می‌کند.',
            reply_markup=_skip_keyboard('weight'),
        )
    elif step == 'weight':
        clean = value.translate(_PERSIAN_DIGITS).replace('٫', '.').replace(',', '').strip()
        if not re.fullmatch(r'\d{1,7}(?:\.\d{1,3})?', clean) or not 0 < float(clean) <= 1000000:
            await chat.send_message('وزن را به گرم و عدد مثبت بفرست؛ مثلاً ۱۵۰.')
            return True
        data = state['data']
        data['weight_grams'] = clean
        await _preview(uid, chat, data)
    else:
        await chat.send_message('از دکمه‌های همین مرحله استفاده کن.')
    return True


async def maybe_photo(update, ctx):
    if not update.message or not update.message.photo:
        return False
    uid = update.effective_user.id
    state = ops.get_state(uid)
    if not state or state.get('flow') != FLOW:
        return False
    if not await m.access(update):
        return True
    lock = _PICTURE_LOCKS.setdefault(uid, asyncio.Lock())
    async with lock:
        state = ops.get_state(uid)
        if not state or state.get('flow') != FLOW or state['step'] != 'photos':
            await update.message.reply_text('مرحله دریافت عکس تمام شده؛ از منو دوباره شروع کن.')
            return True
        data = state['data']
        media_ids = data.get('image_ids') or []
        if len(media_ids) >= 10:
            await update.message.reply_text('حداکثر ۱۰ عکس برای هر مرحله قابل ثبت است.')
            return True
        p = update.message.photo[-1]
        filename = f'vesta_mahak_{uuid.uuid4().hex}.jpg'
        temp = Path(tempfile.gettempdir()) / filename
        try:
            file = await p.get_file(read_timeout=60, connect_timeout=30)
            await file.download_to_drive(custom_path=str(temp), read_timeout=120, connect_timeout=30)
            media = await asyncio.to_thread(BridgeWooClient().upload_media, temp, filename)
            aid = int(media['id'])
            if aid<=0:
                raise ValueError('شناسه رسانه وردپرس نامعتبر است.')
            data.setdefault('image_ids', []).append(aid)
            ops.update_state(uid, data=data)
            count = len(data['image_ids'])
            await update.message.reply_text(
                f'✅ عکس {count} از ۱۰ روی سایت آپلود شد.\n'
                'عکس بعدی را بفرست یا «تصاویر تمام شد» را بزن.',
                reply_markup=_photo_keyboard(),
            )
        except Exception as exc:
            await update.message.reply_text(
                f'❌ عکس منتقل نشد: {exc}\nفقط همان عکس را دوباره بفرست.'
            )
        finally:
            temp.unlink(missing_ok=True)
    return True


async def _after_photo(uid, chat, data):
    ops.update_state(uid, step='caption', data=data)
    current = str((data.get('original') or {}).get('short_description') or '').strip()
    await chat.send_message(
        '✍️ کپشن / توضیح کوتاه محصول را بفرست.\n'
        + (f'کپشن فعلی: {current[:500]}\n\n' if current else '')
        + 'اگر نمی‌خواهی تغییرش بدهی، «حفظ مقدار فعلی» را بزن.',
        reply_markup=_skip_keyboard('caption'),
    )


async def _preview(uid, chat, data):
    ops.update_state(uid, step='preview', data=data)
    selected = data['selected']
    original = data['original']
    caption = data.get('caption')
    grams = data.get('weight_grams')
    imgs = len(data.get('image_ids') or [])
    mode = data.get('cover_mode', 'if_empty')
    if not imgs and caption is None and grams is None:
        await chat.send_message('هیچ تغییری انتخاب نکردی؛ محصول بدون تغییر باقی ماند.', reply_markup=ops.main_menu())
        ops.clear_state(uid)
        return
    await chat.send_message(
        f"🧾 پیش‌نمایش تکمیل {selected['name']}\n"
        f"کد محک: {selected['code']} | سایت: {data['woo_id']}\n\n"
        f'📷 عکس جدید: {imgs} (کاور: {"جایگزین" if mode=="replace" else "حفظ فعلی" if mode=="keep" else "فقط در صورت خالی‌بودن"})\n'
        f'✍️ کپشن: {caption[:450] if caption is not None else "بدون تغییر"}\n'
        f'⚖️ وزن بسته‌بندی: {grams+" گرم" if grams is not None else "بدون تغییر"}\n\n'
        'قیمت، موجودی، کد محصول، تنوع‌ها و وضعیت انتشار تغییر نمی‌کنند. '
        'عکس‌های قبلی پاک نمی‌شوند.',
        reply_markup=_confirm_keyboard(),
    )


async def _commit(uid, chat):
    state = ops.get_state(uid)
    if not state or state.get('flow') != FLOW or state['step'] not in ('preview', 'saving'):
        await chat.send_message('پیش‌نمایش منقضی شده؛ محصول را دوباره انتخاب کن.')
        return
    data = state['data']
    key=data.get('request_key') or secrets.token_hex(16)
    data['request_key']=key
    ops.update_state(uid, step='saving', data=data)
    original=data['original']
    payload={
        'request_key':key,'product_id':data['selected']['product_id'],
        'code':data['selected']['code'],'woo_id':data['woo_id'],
        'image_ids':data.get('image_ids') or [],
        'cover_mode':data.get('cover_mode','if_empty'),
        'expected_image_id':int(original.get('image_id') or 0),
    }
    if data.get('caption') is not None:
        payload['caption']=data['caption']
    if data.get('weight_grams') is not None:
        payload['weight_grams']=data['weight_grams']
    await chat.send_message('در حال اتصال محتوای ربات به همان محصول سایت…')
    try:
        outcome=await asyncio.to_thread(
            BridgeWooClient().call,'mahak_content_save',payload,
        )
    except Exception as exc:
        await chat.send_message(
            f'⚠️ نتیجه ذخیره قطعی نیست: {exc}\n'
            'برای جلوگیری از ثبت دوباره، همان درخواست را با دکمه بررسی/ارسال مجدد پیگیری کن.',
            reply_markup=_retry_keyboard(),
        )
        return
    product=outcome.get('product') or {}
    ops.clear_state(uid)
    await chat.send_message(
        f"✅ محتوای محصول {product.get('name', data['selected']['name'])} تکمیل شد.\n"
        f"شناسه سایت: {product.get('woo_id', data['woo_id'])}\n"
        f"کاور: {'✅' if product.get('image_id') else '—'} | "
        f"گالری: {len(product.get('gallery_image_ids') or [])} عکس\n"
        f"وزن: {product.get('weight_grams') or '—'} گرم\n\n"
        'اطلاعات حسابداری و وضعیت انتشار تغییر نکردند.\n'
        f"{product.get('edit_url', '')}",
        reply_markup=ops.main_menu(),
    )


async def maybe_callback(update, ctx):
    q = update.callback_query
    action = q.data or ''
    if not action.startswith('mahakcontent:'):
        return False
    if not m.allowed(q.from_user.id):
        await q.answer('دسترسی نداری.', show_alert=True)
        return True
    await q.answer()
    uid = q.from_user.id
    chat = q.message.chat
    if action == 'mahakcontent:from_accounting':
        from app import mahak_flow
        job = mahak_flow._last_job(uid)
        code = str((job or {}).get('code') or '')
        if not code:
            await chat.send_message('هنوز کالایی از مسیر ثبت در محک برای این حساب ثبت نشده.')
            return True
        try:
            results = await asyncio.to_thread(
                BridgeWooClient().call, 'mahak_site_search', {'query': code},
            )
            items = [row for row in (results.get('items') or []) if str(row.get('code')) == code]
        except Exception as exc:
            await chat.send_message(f'بررسی محصول محک ناموفق بود: {exc}')
            return True
        if len(items) != 1:
            await chat.send_message('کالای جدید هنوز با شناسه یکتا در خروجی محک پیدا نشده. '
                                    'ابتدا همگام‌سازی بازارا را کامل کن.')
            return True
        await _begin_selected(uid, chat, items[0])
        return True
    state = ops.get_state(uid)
    step = state['step'] if state and state['flow']==FLOW else None

    if action=='mahakcontent:cancel':
        ops.reset_user_flow(uid)
        await chat.send_message('عملیات لغو شد؛ اطلاعات محک و محصول سایت تغییری نکرد.', reply_markup=ops.main_menu())
        return True
    if action=='mahakcontent:again':
        ops.set_state(uid,FLOW,'search',{})
        await chat.send_message('نام یا کد محصول ثبت‌شده در محک را بفرست.', reply_markup=ops.cancel_menu())
        return True
    if action.startswith('mahakcontent:pick:'):
        if step!='select':
            await chat.send_message('این فهرست منقضی شده؛ دوباره جست‌وجو کن.')
            return True
        try:
            pos=int(action.rsplit(':',1)[-1])
            item=state['data']['items'][pos]
        except (IndexError,ValueError,TypeError,KeyError):
            await chat.send_message('محصول نامعتبر است؛ دوباره جست‌وجو کن.')
            return True
        await _begin_selected(uid,chat,item)
        return True
    if not step:
        await chat.send_message('این عملیات منقضی شده؛ از منو شروع کن.')
        return True
    data=state['data']
    if action=='mahakcontent:photos_done' and step=='photos':
        lock=_PICTURE_LOCKS.setdefault(uid,asyncio.Lock())
        async with lock:
            state=ops.get_state(uid)
            if not state or state['step']!='photos':return True
            data=state['data']
            if data.get('image_ids') and int(data['original'].get('image_id') or 0)>0:
                ops.update_state(uid,step='cover_choice',data=data)
                await chat.send_message('این محصول از قبل تصویر اصلی دارد؛ با عکس اول جدید چه کنم؟',
                                        reply_markup=_cover_keyboard())
            else:
                await _after_photo(uid,chat,data)
        return True
    if action.startswith('mahakcontent:cover:') and step=='cover_choice':
        data['cover_mode']='replace' if action.endswith(':replace') else 'keep'
        await _after_photo(uid,chat,data)
        return True
    if action=='mahakcontent:caption_skip' and step=='caption':
        data['caption']=None
        ops.update_state(uid,step='weight',data=data)
        await chat.send_message('⚖️ وزن محصول را به گرم بفرست؛ مثلاً ۱۵۰.',
                                reply_markup=_skip_keyboard('weight'))
        return True
    if action=='mahakcontent:weight_skip' and step=='weight':
        data['weight_grams']=None
        await _preview(uid,chat,data)
        return True
    if action=='mahakcontent:save' and step=='preview':
        await _commit(uid,chat)
        return True
    if action=='mahakcontent:retry' and step=='saving':
        await _commit(uid,chat)
        return True
    await chat.send_message('این دکمه مربوط به مرحله قبلی است.')
    return True
