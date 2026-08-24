from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import main as m
from app import operations as ops
from app import product_ux as pux


def weight_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('⏭ بدون وزن', callback_data='woo:weight:skip'),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


async def text(update, ctx):
    if not await m.access(update):
        return

    uid = update.effective_user.id
    state = ops.get_state(uid)

    if state and state.get('flow') == 'woo_product' and state.get('step') == 'short_description':
        value = (update.message.text or '').strip()
        if not value:
            return await update.message.reply_text(
                'کپشن نمی‌تواند خالی باشد؛ توضیح کوتاه محصول را بفرستید.'
            )

        data = state['data']
        data['short_description'] = value
        ops.update_state(uid, step='weight', data=data)
        return await update.message.reply_text(
            '⚖️ وزن محصول را عددی وارد کنید.\n'
            'عدد باید مطابق واحد وزن تنظیم‌شده در ووکامرس سایت باشد؛ مثال: 0.25 یا 250.\n\n'
            'اگر این محصول وزن ندارد، «بدون وزن» را بزنید.',
            reply_markup=weight_keyboard(),
        )

    return await pux.text(update, ctx)


async def callback(update, ctx):
    q = update.callback_query
    cb = q.data or ''

    if cb != 'woo:weight:skip':
        return await pux.callback(update, ctx)

    uid = q.from_user.id
    if not m.allowed(uid):
        return await q.answer('دسترسی ندارید', show_alert=True)

    state = ops.get_state(uid)
    if not state or state.get('flow') != 'woo_product' or state.get('step') != 'weight':
        return await q.answer('این مرحله منقضی شده است.', show_alert=True)

    await q.answer()
    data = state['data']
    data['weight'] = ''
    ops.update_state(uid, step='categories', data=data)
    await q.edit_message_text('⏭ وزن برای این محصول ثبت نمی‌شود.')
    return await pux.send_categories(q.message.chat, uid)
