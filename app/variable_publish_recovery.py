import asyncio
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import operations as ops
from app import variations as var


def _retry_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('🔁 ادامه ساخت Variationها', callback_data='woo:publish'),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


async def publish_variable_resumable(chat, uid, data):
    """Publish a variable product without losing progress after a slow origin.

    The parent id and every confirmed child are persisted in the Telegram bot
    state immediately. Retrying the publish button therefore resumes the same
    parent instead of creating another parent product.
    """
    client = ops.WooClient()
    attr_name = data.get('attribute_name') or 'گزینه'
    variations = list(data.get('variations') or [])
    values = [v.get('option', '') for v in variations]

    publish_key = data.get('_publish_key')
    if not publish_key:
        publish_key = secrets.token_hex(16)
        data['_publish_key'] = publish_key
        data['_published_variation_indexes'] = []
        ops.update_state(uid, step='preview', data=data)

    parent = data.get('_published_parent')
    if not isinstance(parent, dict) or not parent.get('id'):
        parent_payload = {
            'name': data['name'],
            'type': 'variable',
            'status': 'publish',
            'client_key': f'{publish_key}:parent',
            'categories': [{'id': int(i)} for i in data.get('categories', [])],
            'images': [{'id': int(x['id'])} for x in data.get('images', [])],
            'short_description': str(data.get('short_description') or ''),
            'weight': str(data.get('weight') or ''),
            'attributes': [{
                'name': attr_name,
                'visible': True,
                'variation': True,
                'options': values,
            }],
        }
        try:
            parent = await asyncio.to_thread(client.create_product, parent_payload)
        except Exception as exc:
            ops.update_state(uid, step='preview', data=data)
            await chat.send_message(
                '❌ ساخت محصول مادر کامل تأیید نشد.\n\n'
                f'{exc}\n\n'
                'درخواست خودکار تکرار نشده تا محصول تکراری ساخته نشود. بعد از بررسی سایت، دوباره «ثبت در سایت» را بزنید.',
                reply_markup=_retry_keyboard(),
            )
            return None

        data['_published_parent'] = {
            'id': int(parent['id']),
            'name': parent.get('name') or data.get('name') or '',
            'permalink': parent.get('permalink') or '',
        }
        ops.update_state(uid, step='preview', data=data)

    pid = int(data['_published_parent']['id'])
    completed = {int(x) for x in data.get('_published_variation_indexes', [])}

    for idx, variation in enumerate(variations):
        if idx in completed:
            continue

        payload = {
            'client_key': f'{publish_key}:variation:{idx}',
            'regular_price': str(variation.get('regular_price') or ''),
            'manage_stock': True,
            'stock_quantity': int(variation.get('stock') or 0),
            'attributes': [{'name': attr_name, 'option': variation.get('option', '')}],
        }
        if variation.get('sale_price'):
            payload['sale_price'] = str(variation['sale_price'])

        try:
            await asyncio.to_thread(client.create_variation, pid, payload)
        except Exception as exc:
            data['_published_variation_indexes'] = sorted(completed)
            ops.update_state(uid, step='preview', data=data)
            await chat.send_message(
                f'⚠️ محصول مادر #{pid} روی سایت هست.\n'
                f'Variationهای تأییدشده: {len(completed)}/{len(variations)}\n\n'
                f'ساخت «{variation.get("option", idx + 1)}» تأیید نشد:\n{exc}\n\n'
                'پیشرفت ذخیره شده؛ دکمه زیر را بزنید تا از همین محصول ادامه دهد و محصول مادر جدید نسازد.',
                reply_markup=_retry_keyboard(),
            )
            return None

        completed.add(idx)
        data['_published_variation_indexes'] = sorted(completed)
        ops.update_state(uid, step='preview', data=data)

    parent = data['_published_parent']
    ops.clear_state(uid)
    await chat.send_message(
        '✅ محصول متغیر با موفقیت ثبت شد.\n\n'
        f'#{pid} — {parent.get("name") or data.get("name", "")}\n'
        f'Variationها: {len(completed)}\n'
        f'{parent.get("permalink", "")}',
        reply_markup=ops.woo_menu(),
    )
    return parent


# variations.publish_product resolves this global dynamically at runtime.
var.publish_variable = publish_variable_resumable
