import json
import time

from app import main as m
from app import site_tracking as st
from app import tracking_queue as tq

ORIG_TEXT = tq.text


def _last_hit():
    raw = m.get('site_tracking_last_hit')
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _age_text(ts):
    try:
        age = max(0, int(time.time() - float(ts)))
    except Exception:
        return 'نامشخص'
    if age < 60:
        return f'{age} ثانیه قبل'
    if age < 3600:
        return f'{age // 60} دقیقه قبل'
    return f'{age // 3600} ساعت قبل'


async def text(update, ctx):
    value = (update.message.text or '').strip()
    if value != '📊 وضعیت رهگیری':
        return await ORIG_TEXT(update, ctx)
    if not await m.access(update):
        return

    pending, done, failed = tq._queue_stats()
    result = tq.latest_result()
    hit = _last_hit()

    if hit:
        path = hit.get('path') or '?'
        hit_line = f'آخرین تماس سایت با ربات: {_age_text(hit.get("time"))} ({path})'
    else:
        hit_line = 'آخرین تماس سایت با ربات: ❌ هنوز هیچ Pullی دریافت نشده'

    extra = ''
    if result:
        extra = (
            '\nآخرین ثبت سایت:\n'
            f'کل جدول: {result.get("total", 0)} | '
            f'متصل: {result.get("linked", 0)} | '
            f'بدون اتصال: {result.get("unlinked", 0)}\n'
        )

    return await update.message.reply_text(
        '📊 وضعیت رهگیری سایت\n\n'
        f'در صف برای دریافت توسط سایت: {pending}\n'
        f'ثبت موفق: {done}\n'
        f'خطای فایل محلی: {failed}\n'
        f'{hit_line}\n'
        f'{extra}\n'
        'حالت انتقال: WordPress Pull 2.3.2\n'
        'اگر «آخرین تماس» روی ❌ بماند، مشکل خروجی هاست/اجرای افزونه است؛ اگر زمان تماس نمایش داده شود ولی صف کم نشود، مشکل احراز هویت یا Import است.',
        reply_markup=st.site_tracking_menu(),
    )
