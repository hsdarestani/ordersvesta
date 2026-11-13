"""Import EXISTING Mahak catalog products into WooCommerce drafts.

This is deliberately separate from mahak_flow (which creates NEW accounting
items). A product is selected by name/barcode: its Mahak code is fetched by the
signed bridge and never has to be typed by the Telegram user.
"""
import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import main as m
from app import operations as ops
from app.bridge_client import BridgeWooClient

LABEL = '📥 کالای محک به سایت'
FLOW = 'mahak_site_existing'

NAVIGATION = {
    '❌ لغو عملیات', '⬅️ منوی اصلی', '🛍 مدیریت ووکامرس',
    '📦 رهگیری شاپینو', '⚙️ تنظیمات و اتصال‌ها',
    '🧾 ثبت کالا در محک', '🧾 ساخت کالای جدید در محک',
    '➕ ثبت محصول جدید',
}


def _selection_markup(products):
    rows = []
    for i, product in enumerate(products):
        tag = '✅ در سایت' if product.get('woo_id') else '📥 انتخاب'
        title = str(product.get('name') or '')[:44]
        rows.append([InlineKeyboardButton(
            f'{tag} {i + 1}. {title}', callback_data=f'mahaksite:pick:{i}'
        )])
    rows.append([InlineKeyboardButton('🔍 جست‌وجوی دوباره', callback_data='mahaksite:again')])
    rows.append([InlineKeyboardButton('❌ انصراف', callback_data='mahaksite:cancel')])
    return InlineKeyboardMarkup(rows)


def _confirm_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ انتقال به پیش‌نویس سایت', callback_data='mahaksite:import')],
        [InlineKeyboardButton('🔍 انتخاب محصول دیگر', callback_data='mahaksite:again')],
        [InlineKeyboardButton('❌ انصراف', callback_data='mahaksite:cancel')],
    ])


def _status_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🔎 بررسی نتیجه انتقال', callback_data='mahaksite:status')],
        [InlineKeyboardButton('📥 کالای بعدی', callback_data='mahaksite:again')],
    ])


async def start(update, ctx=None):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    ops.set_state(uid, FLOW, 'search', {})
    await update.effective_chat.send_message(
        '📥 انتقال کالای موجود محک به سایت\n\n'
        'نام کالا یا بارکدش را بنویسید. ربات در لیست کالاهای ثبت‌شده حسابداری جست‌وجو می‌کند؛ '
        'نیازی به رمز محک یا واردکردن کد یکتا نیست.\n\n'
        'این مسیر هیچ کالایی در محک نمی‌سازد و موجودی حسابداری را تغییر نمی‌دهد.',
        reply_markup=ops.cancel_menu(),
    )


async def _search(uid, chat, query):
    if len(query) < 2 or len(query) > 100:
        await chat.send_message('حداقل ۲ و حداکثر ۱۰۰ حرف از نام کالا یا بارکد را بفرستید.')
        return
    status = await chat.send_message('🔎 در حال جست‌وجو میان کالاهای ثبت‌شده محک…')
    try:
        found = await asyncio.to_thread(
            BridgeWooClient().call, 'mahak_site_search', {'query': query},
        )
    except Exception as exc:
        await status.edit_text(
            f'❌ جست‌وجو در اطلاعات محک ناموفق بود: {exc}\n'
            'هیچ کالا یا موجودی‌ای تغییر نکرد. نام را دوباره بفرستید.'
        )
        return
    items = found.get('items') or []
    if not isinstance(items, list):
        items = []
    items = items[:8]
    if not items:
        await status.edit_text(
            'کالایی با این نام یا بارکد در خروجی فعلی محک پیدا نشد.\n'
            'نام کوتاه‌تری بنویسید. اگر کالا تازه در حسابداری ثبت شده، '
            'ممکن است هنوز به بازارا همگام نشده باشد.'
        )
        return
    ops.update_state(uid, step='select', data={'items': items, 'query': query})
    total = int(found.get('total') or len(items))
    lines = ['کالاهای موجود در محک:', '']
    for i, p in enumerate(items, 1):
        where = '✅ در سایت موجود' if p.get('woo_id') else 'هنوز در سایت نیست'
        lines.append(f"{i}. {p.get('name', '')}\n   کد محک: {p.get('code', '')} | {where}")
    if total > len(items):
        lines.append(f'\n{total} نتیجه یافت شد؛ برای دقیق‌تر شدن جست‌وجو، نام کامل‌تری بنویسید.')
    lines.append('\nکالای درست را با دکمه انتخاب کنید.')
    await status.edit_text('\n'.join(lines), reply_markup=_selection_markup(items))


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
    if state.get('step') == 'search':
        await _search(uid, update.effective_chat, value)
    else:
        await update.effective_chat.send_message(
            'برای انتخاب یا ثبت از دکمه‌های مرحله فعلی استفاده کنید؛ '
            'برای جست‌وجوی مجدد دکمه «🔍 جست‌وجوی دوباره» را بزنید.'
        )
    return True


async def _status(uid, chat):
    state = ops.get_state(uid)
    item = ((state or {}).get('data') or {}).get('selected')
    if not state or state.get('flow') != FLOW or not item:
        await chat.send_message('کالایی برای بررسی انتخاب نشده است.')
        return
    try:
        result = await asyncio.to_thread(
            BridgeWooClient().call, 'mahak_site_status',
            {'product_id': item['product_id'], 'code': item['code']},
        )
    except Exception as exc:
        await chat.send_message(f'❌ بررسی وضعیت انتقال ناموفق بود: {exc}')
        return
    await _show_result(chat, item, result)


async def _show_result(chat, item, result):
    status = result.get('status')
    url = result.get('edit_url') or ''
    woo_id = result.get('woo_id')
    if status == 'draft_created':
        message = (
            f"✅ «{item['name']}» به‌صورت پیش‌نویس در ووکامرس ساخته شد.\n"
            f'شناسه سایت: {woo_id}\n'
            'حالا عکس‌ها، قیمت، توضیحات، دسته‌بندی و موجودی را در سایت تکمیل کنید.'
        )
    elif status == 'already_on_site':
        message = (
            f"✅ «{item['name']}» از قبل در ووکامرس وجود دارد (#{woo_id}).\n"
            'برای جلوگیری از تکرار، هیچ محصول جدیدی ساخته نشد.'
        )
    elif status in ('pending', 'uncertain'):
        message = (
            '⚠️ نتیجه انتقال هنوز قطعی نیست؛ برای جلوگیری از ساخت کالای تکراری، '
            'عملیات دوباره ارسال نمی‌شود. دکمه بررسی وضعیت را بزنید.'
        )
    elif status == 'not_imported':
        message = 'هیچ انتقال قبلی برای این کالا ثبت نشده است؛ دوباره آن را از جست‌وجو انتخاب کنید.'
    else:
        message = f"وضعیت انتقال: {status or 'نامشخص'}"
    if url:
        message += f'\n\nویرایش کالا در وردپرس:\n{url}'
    note = result.get('note')
    if note and status in ('pending', 'uncertain'):
        message += '\n' + str(note)[:350]
    await chat.send_message(message, reply_markup=_status_markup())


async def maybe_callback(update, ctx):
    q = update.callback_query
    action = (q.data or '')
    if not action.startswith('mahaksite:'):
        return False
    if not m.allowed(q.from_user.id):
        await q.answer('دسترسی ندارید.', show_alert=True)
        return True
    await q.answer()
    uid = q.from_user.id
    chat = q.message.chat
    state = ops.get_state(uid)
    step = state.get('step') if state and state.get('flow') == FLOW else None
    if action == 'mahaksite:cancel':
        if step:
            ops.reset_user_flow(uid)
        await chat.send_message('عملیات لغو شد.', reply_markup=ops.main_menu())
        return True
    if action == 'mahaksite:again':
        ops.set_state(uid, FLOW, 'search', {})
        await chat.send_message('نام یا بارکد کالای موجود در محک را بفرستید.', reply_markup=ops.cancel_menu())
        return True

    if action.startswith('mahaksite:pick:'):
        if step != 'select':
            await chat.send_message('این فهرست منقضی شده است؛ دوباره جست‌وجو کنید.')
            return True
        try:
            index = int(action.rsplit(':', 1)[-1])
            item = state['data']['items'][index]
        except (ValueError, IndexError, KeyError, TypeError):
            await chat.send_message('انتخاب نامعتبر است؛ دوباره جست‌وجو کنید.')
            return True
        selected = {k: item[k] for k in ('product_id', 'code', 'name')}
        if item.get('woo_id'):
            ops.update_state(uid, step='selected', data={'selected': selected})
            await chat.send_message('این کالا از قبل در سایت موجود است. نسخه جدید نمی‌سازم.')
            await _status(uid, chat)
            return True
        ops.update_state(uid, step='preview', data={'selected': selected})
        count = int(item.get('detail_count') or 0)
        message = (
            'پیش‌نمایش انتقال کالای موجود به سایت:\n\n'
            f"نام: {item['name']}\n"
            f"کد محک: {item['code']}\n"
            f"شناسه حسابداری: {item['product_id']}\n\n"
            'فقط یک پیش‌نویس غیرقابل‌خرید در سایت ساخته می‌شود. '
            'قیمت و موجودی محک کپی نمی‌شود و در حسابداری هیچ تغییری انجام نمی‌گیرد.'
        )
        if count > 1:
            message += '\n\n⚠️ این کالا چند ردیف/تنوع در محک دارد؛ برای جلوگیری از انتقال اشتباه نیاز به بررسی دستی دارد.'
        await chat.send_message(message, reply_markup=_confirm_markup())
        return True

    if action == 'mahaksite:status':
        await _status(uid, chat)
        return True
    if action == 'mahaksite:import':
        if step not in ('preview', 'sending'):
            await chat.send_message('پیش‌نمایش فعال نیست؛ ابتدا کالا را انتخاب کنید.')
            return True
        if step == 'sending':
            await _status(uid, chat)
            return True
        item = state['data']['selected']
        ops.update_state(uid, step='sending', data=state['data'])
        await chat.send_message('در حال بررسی تکراری‌نبودن و ساخت پیش‌نویس سایت…')
        try:
            result = await asyncio.to_thread(
                BridgeWooClient().call, 'mahak_site_import',
                {'product_id': item['product_id'], 'code': item['code']},
            )
        except Exception as exc:
            await chat.send_message(
                f'⚠️ نتیجه قطعی دریافت نشد: {exc}\n'
                'برای جلوگیری از تکرار، درخواست دوباره ارسال نمی‌شود؛ «بررسی نتیجه انتقال» را بزنید.',
                reply_markup=_status_markup(),
            )
            return True
        ops.update_state(uid, step='selected', data=state['data'])
        await _show_result(chat, item, result)
        return True
    return True
