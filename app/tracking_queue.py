import asyncio
import json
import time
import uuid
from pathlib import Path

from app import main as m
from app import operations as ops
from app import routing_hotfix as rh
from app import runner
from app import site_tracking as st

ORIG_TEXT = rh.text
ORIG_DOCUMENT = rh.document

QUEUE_DIR = m.DATA / 'site_tracking_queue'
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

with m.db() as c:
    c.executescript('''
    CREATE TABLE IF NOT EXISTS site_tracking_jobs(
        id TEXT PRIMARY KEY,
        user INTEGER NOT NULL,
        chat INTEGER NOT NULL,
        filename TEXT NOT NULL,
        path TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_retry REAL NOT NULL DEFAULT 0,
        last_error TEXT,
        result_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS idx_site_tracking_jobs_due
        ON site_tracking_jobs(status, next_retry);
    ''')

_WORKER_TASK = None
_ACTIVE = set()


def _row_to_dict(row):
    return dict(row) if row else None


def _job(job_id):
    with m.db() as c:
        return _row_to_dict(c.execute(
            'SELECT * FROM site_tracking_jobs WHERE id=?', (job_id,)
        ).fetchone())


def _queue_stats():
    with m.db() as c:
        pending = c.execute(
            "SELECT COUNT(*) n FROM site_tracking_jobs WHERE status IN ('pending','retry','processing')"
        ).fetchone()['n']
        done = c.execute(
            "SELECT COUNT(*) n FROM site_tracking_jobs WHERE status='done'"
        ).fetchone()['n']
        failed = c.execute(
            "SELECT COUNT(*) n FROM site_tracking_jobs WHERE status='failed'"
        ).fetchone()['n']
    return int(pending), int(done), int(failed)


def _retry_delay(attempts):
    # Fast retries first, then back off. Jobs stay durable across container restarts.
    schedule = (20, 45, 90, 180, 300, 600)
    return schedule[min(max(0, attempts - 1), len(schedule) - 1)]


def _mark_processing(job_id):
    with m.db() as c:
        c.execute(
            "UPDATE site_tracking_jobs SET status='processing',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (job_id,),
        )


def _mark_retry(job_id, attempts, error):
    delay = _retry_delay(attempts)
    with m.db() as c:
        c.execute(
            '''UPDATE site_tracking_jobs
               SET status='retry',attempts=?,next_retry=?,last_error=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=?''',
            (attempts, time.time() + delay, str(error)[:1200], job_id),
        )
    return delay


def _mark_done(job_id, result):
    with m.db() as c:
        c.execute(
            '''UPDATE site_tracking_jobs
               SET status='done',result_json=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=?''',
            (json.dumps(result or {}, ensure_ascii=False), job_id),
        )


async def _notify_success(bot, job, result):
    await bot.send_message(
        job['chat'],
        '✅ فایل رهگیری با موفقیت داخل سایت ثبت شد.\n\n'
        f'فایل: {result.get("filename", job["filename"])}\n'
        f'ردیف‌های فایل: {result.get("rows", 0)}\n'
        f'رکورد جدید: {result.get("inserted", 0)}\n'
        f'به‌روزرسانی/تکراری: {result.get("updated", 0)}\n'
        f'بدون کد رهگیری: {result.get("skipped", 0)}\n'
        f'متصل به سفارش ووکامرس: {result.get("linked", 0)}\n'
        f'بدون اتصال: {result.get("unlinked", 0)}\n'
        f'کل جدول سایت: {result.get("total", 0)} رکورد',
        reply_markup=st.site_tracking_menu(),
    )


async def _process_job(job_id, bot, announce_failure=False):
    if job_id in _ACTIVE:
        return
    job = _job(job_id)
    if not job or job['status'] == 'done':
        return
    path = Path(job['path'])
    if not path.exists():
        with m.db() as c:
            c.execute(
                "UPDATE site_tracking_jobs SET status='failed',last_error='queued file missing',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )
        return

    _ACTIVE.add(job_id)
    _mark_processing(job_id)
    try:
        result = await st.upload_tracking_file(path, job['filename'], None)
        _mark_done(job_id, result)
        path.unlink(missing_ok=True)
        await _notify_success(bot, job, result)
    except Exception as exc:
        fresh = _job(job_id) or job
        attempts = int(fresh.get('attempts') or 0) + 1
        delay = _mark_retry(job_id, attempts, exc)
        # Only the first failure is announced. Later retries are intentionally quiet.
        if announce_failure or attempts == 1:
            try:
                await bot.send_message(
                    job['chat'],
                    '⚠️ سایت الان پاسخ نمی‌دهد، ولی فایل از بین نرفته است.\n\n'
                    f'فایل روی سرور ربات ذخیره شد و {delay} ثانیه دیگر خودکار دوباره تلاش می‌کنم. '
                    'لازم نیست فایل را دوباره بفرستید. وقتی ثبت نهایی انجام شود همینجا پیام می‌دهم.',
                    reply_markup=st.site_tracking_menu(),
                )
            except Exception:
                pass
    finally:
        _ACTIVE.discard(job_id)


async def worker(application):
    # Recover a job that was marked processing when the container restarted.
    with m.db() as c:
        c.execute(
            "UPDATE site_tracking_jobs SET status='retry',next_retry=0 WHERE status='processing'"
        )

    while True:
        try:
            with m.db() as c:
                rows = c.execute(
                    '''SELECT id FROM site_tracking_jobs
                       WHERE status IN ('pending','retry') AND next_retry<=?
                       ORDER BY created_at ASC LIMIT 2''',
                    (time.time(),),
                ).fetchall()
            for row in rows:
                await _process_job(row['id'], application.bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never let the queue worker kill the Telegram bot.
            pass
        await asyncio.sleep(10)


async def startup(application):
    global _WORKER_TASK
    if _WORKER_TASK is None or _WORKER_TASK.done():
        _WORKER_TASK = asyncio.create_task(worker(application))


async def text(update, ctx):
    if not await m.access(update):
        return
    value = (update.message.text or '').strip()

    if value == '📊 وضعیت رهگیری':
        pending, done, failed = _queue_stats()
        msg = await update.message.reply_text(
            '📊 وضعیت رهگیری سایت\n\n'
            f'در صف انتقال: {pending}\n'
            f'انتقال‌های موفق ربات: {done}\n'
            f'خطای دائمی محلی: {failed}\n\n'
            '⏳ آمار جدول خود سایت هم در پس‌زمینه بررسی می‌شود…',
            reply_markup=st.site_tracking_menu(),
        )

        async def remote_status():
            try:
                status = await asyncio.wait_for(st.tracking_status(), timeout=8.0)
                await msg.edit_text(
                    '📊 وضعیت رهگیری سایت\n\n'
                    f'کل رکوردهای سایت: {status.get("total", 0)}\n'
                    f'متصل به سفارش ووکامرس: {status.get("linked", 0)}\n'
                    f'بدون اتصال: {status.get("unlinked", 0)}\n'
                    f'در صف انتقال ربات: {pending}\n'
                    f'نسخه افزونه: {status.get("plugin_version", "?")}',
                    reply_markup=st.site_tracking_menu(),
                )
            except Exception:
                await msg.edit_text(
                    '📊 وضعیت رهگیری سایت\n\n'
                    f'در صف انتقال: {pending}\n'
                    f'انتقال‌های موفق ربات: {done}\n'
                    f'خطای دائمی محلی: {failed}\n\n'
                    '⚠️ سایت فعلاً برای دریافت آمار پاسخ نمی‌دهد؛ صف پس‌زمینه فعال است.',
                    reply_markup=st.site_tracking_menu(),
                )

        asyncio.create_task(remote_status())
        return

    return await ORIG_TEXT(update, ctx)


async def document(update, ctx):
    if not await m.access(update):
        return

    uid = update.effective_user.id
    state = ops.get_state(uid)
    if not state or state.get('flow') != 'site_tracking' or state.get('step') != 'waiting_file':
        return await ORIG_DOCUMENT(update, ctx)

    doc = update.message.document
    filename = doc.file_name or 'tracking.xlsx'
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.xlsx', '.xlsm', '.csv'}:
        return await update.message.reply_text(
            '❌ فقط XLSX / XLSM / CSV بفرستید.', reply_markup=ops.cancel_menu()
        )
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        return await update.message.reply_text('❌ حجم فایل بیشتر از 20MB است.')

    status = await update.message.reply_text('📥 در حال دریافت فایل از تلگرام…')
    job_id = uuid.uuid4().hex
    target = QUEUE_DIR / f'{job_id}{suffix}'
    try:
        await runner.download_telegram_file(doc, target)
        if not target.exists() or target.stat().st_size <= 0:
            raise RuntimeError('فایل دانلودشده خالی است.')

        with m.db() as c:
            c.execute(
                '''INSERT INTO site_tracking_jobs(id,user,chat,filename,path,status,next_retry)
                   VALUES(?,?,?,?,?,'pending',0)''',
                (job_id, uid, update.effective_chat.id, filename, str(target)),
            )

        ops.reset_user_flow(uid)
        await status.edit_text(
            '✅ فایل با اطمینان روی سرور ربات ذخیره شد.\n\n'
            '📤 انتقال به جدول رهگیری سایت در پس‌زمینه انجام می‌شود. '
            'اگر سایت timeout بدهد، ربات خودش دوباره تلاش می‌کند؛ لازم نیست فایل را دوباره ارسال کنید.'
        )
        await update.effective_chat.send_message(
            f'شناسه انتقال: {job_id[:8]}\nوقتی ثبت نهایی روی سایت انجام شود نتیجه را همینجا می‌فرستم.',
            reply_markup=st.site_tracking_menu(),
        )
        asyncio.create_task(_process_job(job_id, ctx.bot, announce_failure=True))
        return
    except Exception as exc:
        target.unlink(missing_ok=True)
        await status.edit_text(f'❌ دریافت/ذخیره فایل ناموفق بود: {exc}')
        return await update.effective_chat.send_message(
            'فایل را دوباره بفرستید یا عملیات را لغو کنید.',
            reply_markup=ops.cancel_menu(),
        )
