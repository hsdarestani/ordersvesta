from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import main as m
from app import sms_admin_access as sms_access


# Keep the original matcher and only enrich its candidates with human-readable data.
_ORIG_CANDIDATES = m.candidates


def _first_value(mapping, keys):
    if not isinstance(mapping, dict):
        return ''
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _display_order_ref(order_shipping):
    order = order_shipping.get('order') or {}
    # Shopino deployments can expose the visible order number under different keys.
    return (
        _first_value(order, ('order_number', 'number', 'code', 'reference', 'reference_number'))
        or _first_value(order_shipping, ('order_number', 'number', 'code', 'reference', 'reference_number'))
    )


def candidates(row, orders):
    base = _ORIG_CANDIDATES(row, orders)
    by_id = {}
    for order_shipping in orders:
        try:
            by_id[int(order_shipping.get('id'))] = order_shipping
        except (TypeError, ValueError):
            continue

    enriched = []
    for candidate in base:
        item = dict(candidate)
        order_shipping = by_id.get(int(item['id']))
        if order_shipping:
            item['display_order_ref'] = _display_order_ref(order_shipping)
            phone = m.sms_phone(order_shipping)
            item['phone_tail'] = phone[-4:] if phone and len(phone) >= 4 else ''
        else:
            item['display_order_ref'] = ''
            item['phone_tail'] = ''
        enriched.append(item)
    return enriched


def _candidate_label(candidate):
    parts = []

    # Always show an order identifier so repeated customers can be distinguished.
    # Prefer Shopino's visible order number when available; otherwise fall back to
    # the order-shipping ID that the old UI showed and that the callback already uses.
    ref = str(candidate.get('display_order_ref') or candidate.get('id') or '').strip()
    if ref:
        parts.append(f'سفارش #{ref}')

    name = str(candidate.get('name') or 'بدون نام').strip()
    city = str(candidate.get('city') or '').strip()
    customer = name + (f' | {city}' if city else '')
    parts.append(customer)

    phone_tail = str(candidate.get('phone_tail') or '').strip()
    if phone_tail:
        parts.append(f'موبایل ••••{phone_tail}')

    return ' | '.join(parts)[:64]


def keyboard(token, candidate_list):
    rows = []
    for candidate in candidate_list[:5]:
        rows.append([
            InlineKeyboardButton(
                _candidate_label(candidate),
                callback_data=f'p:{token}:{candidate["id"]}',
            )
        ])

    if candidate_list:
        skip_text = '⏭ هیچ‌کدام / رد این ردیف'
    else:
        skip_text = '⏭ سفارش مناسب پیدا نشد؛ رد این ردیف'
    rows.append([InlineKeyboardButton(skip_text, callback_data=f's:{token}')])
    return InlineKeyboardMarkup(rows)


def review_text(payload):
    row = payload['row']
    candidate_list = payload.get('candidates') or []
    header = (
        f"⚠️ تطبیق قطعی نیست\n"
        f"مشتری فایل: {row.get('name', '')} | {row.get('city', '')}\n"
        f"کد رهگیری: {row.get('code', '')}\n\n"
    )
    if candidate_list:
        return header + 'یکی از سفارش‌های پیشنهادی را انتخاب کنید. اگر هیچ‌کدام درست نیست، «رد این ردیف» را بزنید.'
    return header + 'سفارش مطمئنی برای این ردیف پیدا نشد. برای جلوگیری از ثبت اشتباه، این ردیف را رد کنید.'


async def ask(chat, token, payload):
    await chat.send_message(
        review_text(payload),
        reply_markup=keyboard(token, payload.get('candidates') or []),
    )


async def callback(update, ctx):
    q = update.callback_query
    cb = q.data or ''

    # Old messages may still contain the former "manual ID" button after a deploy.
    # Re-render those messages using the current review UI.
    if cb.startswith('m:'):
        if not m.allowed(q.from_user.id):
            return await q.answer('دسترسی ندارید', show_alert=True)
        await q.answer()
        token = cb.split(':', 1)[1]
        item = m.pending(token)
        if not item or item['status'] != 'pending':
            return await q.edit_message_text('این مورد قبلاً بررسی شده.')
        payload = item['payload']
        return await q.edit_message_text(
            review_text(payload),
            reply_markup=keyboard(token, payload.get('candidates') or []),
        )

    return await sms_access.callback(update, ctx)


# New imports use the improved matcher/review UI immediately.
m.candidates = candidates
m.keyboard = keyboard
m.ask = ask

# Remove stale pre-deploy manual-ID states while preserving SMS scheduling states.
with m.db() as c:
    c.execute("DELETE FROM manual WHERE token NOT LIKE 'schedule:%'")
