import asyncio
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import operations as ops
from app import variations as var


def _retry_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('🔁 بررسی/ادامه ثبت محصول', callback_data='woo:publish'),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


async def publish_simple_resumable(chat, uid):
    """Publish a simple product idempotently and keep state after uncertain timeouts.

    A stable client_key is persisted before the first remote mutation. The
    WordPress Bridge stores that key with the WooCommerce product, so pressing
    publish again after a timeout returns the already-created product instead of
    creating a duplicate.
    """
    state = ops.get_state(uid)
    if not state or state.get('flow') != 'woo_product':
        return None

    data = state.get('data') or {}
    if data.get('type') == 'variable':
        # This wrapper is only for simple products. Keep the variable dispatcher
        # available if routing changes in a future refactor.
        return await var.publish_variable(chat, uid, data)

    publish_key = data.get('_publish_key')
    if not publish_key:
        publish_key = secrets.token_hex(16)
        data['_publish_key'] = publish_key
        ops.update_state(uid, step='preview', data=data)

    payload = {
        'client_key': f'{publish_key}:parent',
        'name': data['name'],
        'type': 'simple',
        'status': 'publish',
        'categories': [{'id': int(i)} for i in data.get('categories', [])],
        'images': [{'id': int(x['id'])} for x in data.get('images', [])],
        'manage_stock': True,
        'stock_quantity': int(data.get('stock', 0)),
        'regular_price': str(data.get('regular_price', '')),
        'short_description': str(data.get('short_description') or ''),
        'weight': str(data.get('weight') or ''),
    }
    if data.get('sale_price'):
        payload['sale_price'] = str(data['sale_price'])

    try:
        product = await asyncio.to_thread(ops.WooClient().create_product, payload)
    except Exception as exc:
        # Do not clear the state. The same client_key must be used on retry so an
        # uncertain timeout can be reconciled against the product saved in WP.
        ops.update_state(uid, step='preview', data=data)
        await chat.send_message(
            '⚠️ نتیجه ثبت محصول هنوز قطعی نیست.\n\n'
            f'{exc}\n\n'
            'اگر ووکامرس درخواست را ذخیره کرده باشد، دکمه زیر همان محصول را پیدا می‌کند؛ '
            'محصول تکراری ساخته نمی‌شود.',
            reply_markup=_retry_keyboard(),
        )
        return None

    ops.clear_state(uid)
    recovered = bool(product.get('already_exists'))
    prefix = '✅ محصول قبلاً روی سایت ثبت شده بود و بازیابی شد.' if recovered else '✅ محصول ثبت شد.'
    await chat.send_message(
        f'{prefix}\n\n'
        f'#{product.get("id")} — {product.get("name")}\n'
        f'{product.get("permalink", "")}',
        reply_markup=ops.woo_menu(),
    )
    return product


# product_ux installs its simple publisher into this dynamic slot. Replace only
# that slot; variable_publish_recovery separately owns var.publish_variable.
var.ORIG_PUBLISH = publish_simple_resumable
