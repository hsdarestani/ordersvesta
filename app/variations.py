import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import operations as ops
from app import main as m

ORIG_TEXT = ops.text
ORIG_CALLBACK = ops.callback


def price_mode_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('💰 قیمت همه یکسان', callback_data='woo:var:price:same'),
        InlineKeyboardButton('🧾 قیمت جداگانه', callback_data='woo:var:price:separate'),
    ]])


def stock_mode_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('📦 موجودی همه یکسان', callback_data='woo:var:stock:same'),
        InlineKeyboardButton('🔢 موجودی جداگانه', callback_data='woo:var:stock:separate'),
    ]])


def variation_sale_keyboard(index=None, common=False):
    cb = 'woo:var:sale:common:skip' if common else f'woo:var:sale:{index}:skip'
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('بدون تخفیف', callback_data=cb),
        InlineKeyboardButton('❌ لغو', callback_data='woo:cancel'),
    ]])


def parse_int(text):
    x = str(text or '').translate(m.DIGITS).replace(',', '').strip()
    return int(x) if x.isdigit() else None


def variations_summary(data):
    rows = []
    for v in data.get('variations', []):
        sale = v.get('sale_price') or '—'
        rows.append(
            f"• {v.get('option')}: موجودی {v.get('stock', 0)} | قیمت {v.get('regular_price', '—')} | تخفیف {sale}"
        )
    return '\n'.join(rows)


def ensure_variations(data):
    return data.setdefault('variations', [])


async def ask_stock_mode(chat, uid, data):
    ops.update_state(uid, step='var_stock_mode', data=data)
    await chat.send_message(
        '📦 موجودی Variationها چطور است؟\n\n'
        'اگر همه یک تعداد دارند «موجودی همه یکسان» را بزنید؛ '
        'اگر فرق دارند «موجودی جداگانه» را انتخاب کنید.',
        reply_markup=stock_mode_keyboard(),
    )


async def finish_variations_to_categories(chat, uid, data):
    # Parent stock/price are not used for variable products; each child variation owns them.
    data['stock'] = sum(int(v.get('stock', 0)) for v in data.get('variations', []))
    ops.update_state(uid, step='categories', data=data)
    await chat.send_message(
        '✅ Variationها کامل شدند:\n\n' + variations_summary(data) + '\n\nحالا دسته‌بندی‌ها را انتخاب کنید.'
    )
    await ops.send_categories(chat, uid)


async def text(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    state = ops.get_state(uid)
    if not state or state.get('flow') != 'woo_product' or state.get('data', {}).get('type') != 'variable':
        return await ORIG_TEXT(update, ctx)

    data = state['data']
    step = state['step']
    text_value = (update.message.text or '').strip()

    # Let normal menu/cancel/category behavior pass through.
    if text_value in {'❌ لغو عملیات', '⬅️ منوی اصلی'} or step in {'name', 'type', 'photos', 'categories', 'preview'}:
        return await ORIG_TEXT(update, ctx)

    if step == 'var_attribute_name':
        if not text_value:
            return await update.message.reply_text('نام ویژگی را وارد کنید؛ مثلاً «رنگ» یا «حجم».')
        data['attribute_name'] = text_value
        ops.update_state(uid, step='var_attribute_values', data=data)
        return await update.message.reply_text(
            f'مقادیر «{text_value}» را بفرستید.\n'
            'با ویرگول یا هرکدام در یک خط. مثال:\nقرمز، صورتی، نود'
        )

    if step == 'var_attribute_values':
        raw = text_value.replace('\n', ',').replace('،', ',')
        values = []
        seen = set()
        for part in raw.split(','):
            value = part.strip()
            key = m.norm(value, True)
            if value and key and key not in seen:
                seen.add(key)
                values.append(value)
        if len(values) < 2:
            return await update.message.reply_text('برای محصول متغیر حداقل دو مقدار بفرستید؛ مثلاً «قرمز، صورتی».')
        data['attribute_values'] = values
        data['variations'] = [{'option': x, 'regular_price': '', 'sale_price': '', 'stock': 0} for x in values]
        ops.update_state(uid, step='var_price_mode', data=data)
        return await update.message.reply_text(
            '💰 قیمت Variationها چطور است؟\n\n'
            'اگر قیمت همه یکی است یک بار می‌گیریم و روی همه اعمال می‌کنیم؛ '
            'اگر فرق دارد یکی‌یکی می‌پرسیم.',
            reply_markup=price_mode_keyboard(),
        )

    if step == 'var_regular_common':
        value = parse_int(text_value)
        if value is None:
            return await update.message.reply_text('قیمت اصلی را فقط به صورت عدد وارد کنید.')
        data['var_common_regular'] = str(value)
        ops.update_state(uid, step='var_sale_common', data=data)
        return await update.message.reply_text(
            'قیمت تخفیف مشترک را بفرستید؛ اگر تخفیف ندارند «بدون تخفیف» را بزنید.',
            reply_markup=variation_sale_keyboard(common=True),
        )

    if step == 'var_sale_common':
        value = parse_int(text_value)
        if value is None:
            return await update.message.reply_text('قیمت تخفیف را فقط به صورت عدد وارد کنید.')
        if value >= int(data['var_common_regular']):
            return await update.message.reply_text('قیمت تخفیف باید از قیمت اصلی کمتر باشد.')
        for v in ensure_variations(data):
            v['regular_price'] = data['var_common_regular']
            v['sale_price'] = str(value)
        data.pop('var_common_regular', None)
        await ask_stock_mode(update.effective_chat, uid, data)
        return

    if step == 'var_regular_each':
        idx = int(data.get('var_index', 0))
        variants = ensure_variations(data)
        if idx >= len(variants):
            return await ask_stock_mode(update.effective_chat, uid, data)
        value = parse_int(text_value)
        if value is None:
            return await update.message.reply_text('قیمت اصلی را فقط به صورت عدد وارد کنید.')
        variants[idx]['regular_price'] = str(value)
        ops.update_state(uid, step='var_sale_each', data=data)
        return await update.message.reply_text(
            f'قیمت تخفیف «{variants[idx]["option"]}» را بفرستید؛ یا «بدون تخفیف» را بزنید.',
            reply_markup=variation_sale_keyboard(index=idx),
        )

    if step == 'var_sale_each':
        idx = int(data.get('var_index', 0))
        variants = ensure_variations(data)
        if idx >= len(variants):
            return await ask_stock_mode(update.effective_chat, uid, data)
        value = parse_int(text_value)
        if value is None:
            return await update.message.reply_text('قیمت تخفیف را فقط به صورت عدد وارد کنید.')
        if value >= int(variants[idx]['regular_price']):
            return await update.message.reply_text('قیمت تخفیف باید از قیمت اصلی کمتر باشد.')
        variants[idx]['sale_price'] = str(value)
        idx += 1
        data['var_index'] = idx
        if idx >= len(variants):
            return await ask_stock_mode(update.effective_chat, uid, data)
        ops.update_state(uid, step='var_regular_each', data=data)
        return await update.message.reply_text(f'قیمت اصلی «{variants[idx]["option"]}» را بفرستید.')

    if step == 'var_stock_common':
        value = parse_int(text_value)
        if value is None:
            return await update.message.reply_text('موجودی را فقط به صورت عدد وارد کنید.')
        for v in ensure_variations(data):
            v['stock'] = value
        return await finish_variations_to_categories(update.effective_chat, uid, data)

    if step == 'var_stock_each':
        idx = int(data.get('var_index', 0))
        variants = ensure_variations(data)
        value = parse_int(text_value)
        if value is None:
            return await update.message.reply_text('موجودی را فقط به صورت عدد وارد کنید.')
        variants[idx]['stock'] = value
        idx += 1
        data['var_index'] = idx
        if idx >= len(variants):
            return await finish_variations_to_categories(update.effective_chat, uid, data)
        ops.update_state(uid, step='var_stock_each', data=data)
        return await update.message.reply_text(f'موجودی «{variants[idx]["option"]}» چند عدد است؟')

    return await ORIG_TEXT(update, ctx)


async def callback(update, ctx):
    q = update.callback_query
    cb = q.data or ''
    uid = q.from_user.id
    state = ops.get_state(uid)

    # Variable product: after photos, start attribute/variation builder instead of parent stock/price.
    if cb == 'woo:photos:done' and state and state.get('flow') == 'woo_product' and state.get('data', {}).get('type') == 'variable':
        if not m.allowed(uid):
            return await q.answer('دسترسی ندارید', show_alert=True)
        await q.answer()
        data = state['data']
        if not data.get('images'):
            return await q.answer('حداقل یک عکس لازم است.', show_alert=True)
        ops.update_state(uid, step='var_attribute_name', data=data)
        await q.edit_message_text(f'✅ {len(data["images"])} تصویر ثبت شد.')
        return await q.message.reply_text(
            '🎛 محصول متغیر است.\n\nنام ویژگی Variation را بفرستید؛ مثلاً:\nرنگ\nحجم\nمدل'
        )

    if not cb.startswith('woo:var:'):
        return await ORIG_CALLBACK(update, ctx)

    if not m.allowed(uid):
        return await q.answer('دسترسی ندارید', show_alert=True)
    await q.answer()
    if not state or state.get('flow') != 'woo_product' or state.get('data', {}).get('type') != 'variable':
        return await q.edit_message_text('این عملیات منقضی شده؛ دوباره ثبت محصول را شروع کنید.')
    data = state['data']
    variants = ensure_variations(data)

    if cb == 'woo:var:price:same':
        ops.update_state(uid, step='var_regular_common', data=data)
        await q.edit_message_text('💰 قیمت همه Variationها یکسان است.')
        return await q.message.reply_text('قیمت اصلی مشترک را بفرستید.')

    if cb == 'woo:var:price:separate':
        data['var_index'] = 0
        ops.update_state(uid, step='var_regular_each', data=data)
        await q.edit_message_text('🧾 قیمت هر Variation جداگانه ثبت می‌شود.')
        return await q.message.reply_text(f'قیمت اصلی «{variants[0]["option"]}» را بفرستید.')

    if cb == 'woo:var:sale:common:skip':
        regular = data.get('var_common_regular', '')
        for v in variants:
            v['regular_price'] = regular
            v['sale_price'] = ''
        data.pop('var_common_regular', None)
        await q.edit_message_text('بدون تخفیف برای همه Variationها ثبت شد.')
        return await ask_stock_mode(q.message.chat, uid, data)

    if cb.startswith('woo:var:sale:') and cb.endswith(':skip'):
        parts = cb.split(':')
        idx = int(parts[3])
        if idx < len(variants):
            variants[idx]['sale_price'] = ''
        idx += 1
        data['var_index'] = idx
        await q.edit_message_text('بدون تخفیف ثبت شد.')
        if idx >= len(variants):
            return await ask_stock_mode(q.message.chat, uid, data)
        ops.update_state(uid, step='var_regular_each', data=data)
        return await q.message.reply_text(f'قیمت اصلی «{variants[idx]["option"]}» را بفرستید.')

    if cb == 'woo:var:stock:same':
        ops.update_state(uid, step='var_stock_common', data=data)
        await q.edit_message_text('📦 موجودی همه Variationها یکسان است.')
        return await q.message.reply_text('موجودی مشترک هر Variation چند عدد است؟')

    if cb == 'woo:var:stock:separate':
        data['var_index'] = 0
        ops.update_state(uid, step='var_stock_each', data=data)
        await q.edit_message_text('🔢 موجودی هر Variation جداگانه ثبت می‌شود.')
        return await q.message.reply_text(f'موجودی «{variants[0]["option"]}» چند عدد است؟')

    return await ORIG_CALLBACK(update, ctx)


def variable_preview_text(data, base_text):
    if data.get('type') != 'variable':
        return base_text
    return (
        base_text + '\n\n🎛 ویژگی: ' + str(data.get('attribute_name', '')) +
        '\n' + variations_summary(data)
    )


# Patch Woo client with variation creation.
def create_variation(self, product_id, payload):
    return self.wc('POST', f'products/{product_id}/variations', json=payload).json()


ops.WooClient.create_variation = create_variation


async def publish_variable(chat, uid, data):
    client = ops.WooClient()
    attr_name = data.get('attribute_name') or 'گزینه'
    values = [v['option'] for v in data.get('variations', [])]
    parent_payload = {
        'name': data['name'],
        'type': 'variable',
        'status': 'publish',
        'categories': [{'id': int(i)} for i in data.get('categories', [])],
        'images': [{'id': int(x['id'])} for x in data.get('images', [])],
        'attributes': [{
            'name': attr_name,
            'visible': True,
            'variation': True,
            'options': values,
        }],
    }
    product = await asyncio.to_thread(client.create_product, parent_payload)
    pid = int(product['id'])
    created = []
    try:
        for v in data.get('variations', []):
            payload = {
                'regular_price': str(v.get('regular_price') or ''),
                'manage_stock': True,
                'stock_quantity': int(v.get('stock') or 0),
                'attributes': [{'name': attr_name, 'option': v['option']}],
            }
            if v.get('sale_price'):
                payload['sale_price'] = str(v['sale_price'])
            child = await asyncio.to_thread(client.create_variation, pid, payload)
            created.append(child)
    except Exception as exc:
        # Parent remains available in WooCommerce for manual recovery; report exact progress.
        raise RuntimeError(
            f'محصول مادر #{pid} ساخته شد ولی ساخت Variationها در {len(created)}/{len(values)} متوقف شد: {exc}'
        )
    ops.clear_state(uid)
    await chat.send_message(
        f'✅ محصول متغیر با موفقیت ثبت شد.\n\n'
        f'#{pid} — {product.get("name")}\n'
        f'Variationها: {len(created)}\n'
        f'{product.get("permalink", "")}',
        reply_markup=ops.woo_menu(),
    )
    return product


# Preserve original simple publish, but fully create child variations for variable products.
ORIG_PUBLISH = ops.publish_product


async def publish_product(chat, uid):
    state = ops.get_state(uid)
    if state and state.get('data', {}).get('type') == 'variable':
        return await publish_variable(chat, uid, state['data'])
    return await ORIG_PUBLISH(chat, uid)


ops.publish_product = publish_product
