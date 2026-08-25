import asyncio
import hashlib
import secrets
import tempfile
import uuid
from pathlib import Path

from telegram import ReplyKeyboardMarkup

from app import bridge_client as bridge
from app import main as m
from app import operations as ops
from app import product_ux as pux
from app import runner

ORIG_TEXT = pux.text
ORIG_DOCUMENT = m.document

# The shop WAF treats bursts of large signed query strings as abusive traffic.
# Tracking uploads must be deliberately sequential so they never throttle/block
# the bot server IP and break unrelated WooCommerce operations afterwards.
TRACKING_CHUNK_SIZE = 3072
TRACKING_CHUNK_CONCURRENCY = 1
TRACKING_CHUNK_PAUSE_SECONDS = 0.15


def main_menu():
    return ReplyKeyboardMarkup([
        ['📦 رهگیری شاپینو', '📦 مدیریت ارسال‌ها'],
        ['🛍 مدیریت ووکامرس', '⚙️ تنظیمات و اتصال‌ها'],
        ['👥 کاربران و دسترسی'],
    ], resize_keyboard=True)


def site_tracking_menu():
    return ReplyKeyboardMarkup([
        ['📤 آپلود اکسل کد رهگیری', '📊 وضعیت رهگیری'],
        ['⬅️ منوی اصلی'],
    ], resize_keyboard=True)


# start() and every existing "back to main" path resolve this function dynamically.
ops.main_menu = main_menu


def _progress_bar(percent, width=10):
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    return f"{'█' * filled}{'░' * (width - filled)} {percent}%"


async def _safe_edit(message, text):
    try:
        await message.edit_text(text)
    except Exception:
        pass


async def tracking_status():
    client = ops.WooClient()
    return await asyncio.to_thread(client._signed_get, 'vpt_status')


async def upload_tracking_file(path, filename, progress_message=None):
    data = Path(path).read_bytes()
    if not data:
        raise RuntimeError('فایل خالی است.')
    if len(data) > 20 * 1024 * 1024:
        raise RuntimeError('حجم فایل بیشتر از 20MB است.')

    upload_id = secrets.token_hex(16)
    digest = hashlib.sha256(data).hexdigest()
    safe_name = Path(filename or str(path)).name or 'tracking.xlsx'
    client = ops.WooClient()

    begin = await asyncio.to_thread(
        client._signed_get,
        'vpt_import_begin',
        {
            'upload_id': upload_id,
            'filename': safe_name,
            'size': len(data),
            'sha256': digest,
            # Same behavior as the plugin's admin uploader with Auto Match enabled:
            # populate its tracking table and link/sync Woo orders when possible.
            'auto_match': True,
        },
    )
    if begin.get('already_finished') and isinstance(begin.get('result'), dict):
        return begin['result']

    chunks = [
        (offset, data[offset:offset + TRACKING_CHUNK_SIZE])
        for offset in range(0, len(data), TRACKING_CHUNK_SIZE)
    ]
    total = len(chunks)
    sem = asyncio.Semaphore(TRACKING_CHUNK_CONCURRENCY)

    async def send_one(offset, chunk):
        async with sem:
            result = await asyncio.to_thread(
                client._signed_get,
                'vpt_import_chunk',
                {
                    'upload_id': upload_id,
                    'offset': offset,
                    'data': bridge._b64url(chunk),
                },
            )
            await asyncio.sleep(TRACKING_CHUNK_PAUSE_SECONDS)
            return result

    tasks = [asyncio.create_task(send_one(offset, chunk)) for offset, chunk in chunks]
    completed = 0
    last_percent = -1
    try:
        for task in asyncio.as_completed(tasks):
            await task
            completed += 1
            # File transfer occupies 15..80 percent. Avoid excessive Telegram edits.
            percent = 15 + round(65 * completed / max(1, total))
            if progress_message and (percent >= last_percent + 5 or completed == total):
                last_percent = percent
                await _safe_edit(
                    progress_message,
                    '📤 در حال انتقال فایل رهگیری به سایت…\n'
                    f'{completed}/{total} بخش\n{_progress_bar(percent)}',
                )
    except Exception:
        # A failed chunk must stop the rest immediately; continuing queued tasks
        # can turn a temporary WAF limit into a longer block.
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    if progress_message:
        await _safe_edit(
            progress_message,
            '⚙️ فایل کامل به سایت رسید.\n'
            'در حال خواندن اکسل، ساخت/آپدیت جدول و اتصال به سفارش‌های ووکامرس…\n'
            f'{_progress_bar(85)}',
        )

    return await asyncio.to_thread(
        client._signed_get,
        'vpt_import_finish',
        {'upload_id': upload_id},
    )


async def text(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    value = (update.message.text or '').strip()

    if value == '📦 مدیریت ارسال‌ها':
        ops.reset_user_flow(uid)
        return await update.message.reply_text(
            '📮 رهگیری پستی سایت\n\n'
            'از این بخش همان فایل XLSX/XLSM/CSV که قبلاً داخل افزونه سایت آپلود می‌کردید را مستقیم از تلگرام می‌فرستید.',
            reply_markup=site_tracking_menu(),
        )

    if value == '📤 آپلود اکسل کد رهگیری':
        # Set the route before any network access. Previously a failed preflight
        # left no state, causing the next spreadsheet to fall into Shopino import.
        ops.set_state(uid, 'site_tracking', 'waiting_file', {})
        return await update.message.reply_text(
            '📤 فایل رهگیری را بفرستید.\n\n'
            'فرمت‌های قابل قبول: XLSX / XLSM / CSV\n'
            'بعد از دریافت، ربات فایل را در همان جدول افزونه سایت وارد می‌کند و تا جای ممکن به سفارش‌های ووکامرس متصل می‌کند.',
            reply_markup=ops.cancel_menu(),
        )

    if value == '📊 وضعیت رهگیری':
        try:
            status = await tracking_status()
            return await update.message.reply_text(
                '📊 وضعیت رهگیری پستی سایت\n\n'
                f'کل رکوردها: {status.get("total", 0)}\n'
                f'متصل به سفارش: {status.get("linked", 0)}\n'
                f'بدون اتصال: {status.get("unlinked", 0)}\n'
                f'نسخه افزونه: {status.get("plugin_version", "?")}',
                reply_markup=site_tracking_menu(),
            )
        except Exception as exc:
            return await update.message.reply_text(
                f'❌ دریافت وضعیت رهگیری سایت ناموفق بود:\n{exc}',
                reply_markup=site_tracking_menu(),
            )

    state = ops.get_state(uid)
    if state and state.get('flow') == 'site_tracking':
        if value in {'❌ لغو عملیات', '⬅️ منوی اصلی'}:
            ops.reset_user_flow(uid)
            return await update.message.reply_text('منوی اصلی:', reply_markup=main_menu())
        return await update.message.reply_text(
            'فایل XLSX/XLSM/CSV را همینجا ارسال کنید یا عملیات را لغو کنید.',
            reply_markup=ops.cancel_menu(),
        )

    return await ORIG_TEXT(update, ctx)


async def document(update, ctx):
    uid = update.effective_user.id
    state = ops.get_state(uid)
    if not state or state.get('flow') != 'site_tracking' or state.get('step') != 'waiting_file':
        return await ORIG_DOCUMENT(update, ctx)
    if not await m.access(update):
        return

    doc = update.message.document
    filename = doc.file_name or 'tracking.xlsx'
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.xlsx', '.xlsm', '.csv'}:
        return await update.message.reply_text(
            '❌ فقط XLSX / XLSM / CSV بفرستید.',
            reply_markup=ops.cancel_menu(),
        )
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        return await update.message.reply_text('❌ حجم فایل بیشتر از 20MB است.')

    msg = await update.message.reply_text(
        '📥 در حال دریافت فایل از تلگرام…\n' + _progress_bar(5)
    )
    tmp = Path(tempfile.gettempdir()) / f'vpt_{uuid.uuid4().hex}{suffix}'
    try:
        await runner.download_telegram_file(doc, tmp)
        await _safe_edit(msg, '✅ فایل دریافت شد.\n📤 در حال ارسال امن به سایت…\n' + _progress_bar(15))
        result = await upload_tracking_file(tmp, filename, msg)
        ops.reset_user_flow(uid)
        await _safe_edit(msg, '✅ ورود فایل به سایت کامل شد.\n' + _progress_bar(100))
        return await update.effective_chat.send_message(
            '📮 نتیجه ثبت رهگیری در سایت\n\n'
            f'فایل: {result.get("filename", filename)}\n'
            f'ردیف‌های فایل: {result.get("rows", 0)}\n'
            f'رکورد جدید: {result.get("inserted", 0)}\n'
            f'به‌روزرسانی/تکراری: {result.get("updated", 0)}\n'
            f'بدون کد رهگیری: {result.get("skipped", 0)}\n'
            f'متصل به سفارش: {result.get("linked", 0)}\n'
            f'بدون اتصال: {result.get("unlinked", 0)}\n\n'
            f'کل جدول سایت: {result.get("total", 0)} رکورد',
            reply_markup=site_tracking_menu(),
        )
    except Exception as exc:
        # Keep the state so the admin can simply resend the same file.
        ops.update_state(uid, step='waiting_file', data=state.get('data', {}))
        await _safe_edit(msg, f'❌ ثبت فایل رهگیری سایت ناموفق بود:\n{exc}')
        return await update.effective_chat.send_message(
            'فایل را دوباره بفرستید یا عملیات را لغو کنید.',
            reply_markup=ops.cancel_menu(),
        )
    finally:
        tmp.unlink(missing_ok=True)
