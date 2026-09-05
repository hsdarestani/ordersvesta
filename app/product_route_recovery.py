import asyncio

from app import main as m
from app import operations as ops

# Set by entrypoint after the complete text-router chain has been composed.
ORIG_TEXT = None


async def _reload_category_cache(uid, state):
    """Repair a product flow that reached categories before the Bridge was healthy.

    Older runs could leave step=categories with no category_cache after a transient
    Bridge/WordPress error. Every later search then ran against an empty list and
    misleadingly answered "no close match" forever. Reload lazily on the next text.
    """
    cats = await asyncio.to_thread(ops.WooClient().categories)
    data = dict(state.get('data') or {})
    data['category_cache'] = [
        {
            'id': int(item['id']),
            'name': str(item.get('name') or ''),
            'parent': int(item.get('parent') or 0),
        }
        for item in cats
        if item.get('id') is not None
    ]
    ops.update_state(uid, step='categories', data=data)
    return data


async def text(update, ctx):
    if not await m.access(update):
        return

    uid = update.effective_user.id
    value = (update.message.text or '').strip()

    # Top-level navigation must always win over an unfinished product wizard.
    # This specifically prevents "⬅️ منوی اصلی" from being interpreted as a
    # category search query when the user is stuck on the categories step.
    if value in {'❌ لغو عملیات', '⬅️ منوی اصلی'}:
        ops.reset_user_flow(uid)
        return await update.message.reply_text('منوی اصلی:', reply_markup=ops.main_menu())

    top_navigation = {
        '📦 رهگیری شاپینو': ('بخش رهگیری شاپینو:', ops.shopino_menu),
        '🛍 مدیریت ووکامرس': ('مدیریت ووکامرس:', ops.woo_menu),
        '⚙️ تنظیمات و اتصال‌ها': ('تنظیمات و اتصال‌ها:', ops.settings_menu),
    }
    if value in top_navigation:
        ops.reset_user_flow(uid)
        label, menu_factory = top_navigation[value]
        return await update.message.reply_text(label, reply_markup=menu_factory())

    state = ops.get_state(uid)
    if (
        state
        and state.get('flow') == 'woo_product'
        and state.get('step') == 'categories'
        and not (state.get('data') or {}).get('category_cache')
    ):
        try:
            await _reload_category_cache(uid, state)
        except Exception as exc:
            return await update.message.reply_text(
                '❌ لیست دسته‌بندی‌ها هنوز از سایت دریافت نشد.\n'
                f'{exc}\n\n'
                'همین عبارت را دوباره بفرستید؛ ربات در تلاش بعدی دوباره لیست را از سایت می‌گیرد.'
            )

    if ORIG_TEXT is None:
        raise RuntimeError('product_route_recovery.ORIG_TEXT is not configured')
    return await ORIG_TEXT(update, ctx)
