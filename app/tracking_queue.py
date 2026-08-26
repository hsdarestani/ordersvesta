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
    cols = {row['name'] for row in c.execute('PRAGMA table_info(site_tracking_jobs)').fetchall()}
    if 'notified' not in cols:
        c.execute('ALTER TABLE site_tracking_jobs ADD COLUMN notified INTEGER NOT NULL DEFAULT 0')
    # Old push-mode jobs must become available to the new site-pull worker.
    c.execute("UPDATE site_tracking_jobs SET status='pending',next_retry=0 WHERE status IN ('processing','retry')")

_WORKER_TASK = None


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
            "SELECT COUNT(*) n FROM site_tracking_jobs WHERE status IN ('pending','leased','retry')"
        ).fetchone()['n']
        done = c.execute(
            "SELECT COUNT(*) n FROM site_tracking_jobs WHERE status='done'"
        ).fetchone()['n']
        failed = c.execute(
            "SELECT COUNT(*) n FROM site_tracking_jobs WHERE status='failed'"
        ).fetchone()['n']
    return int(pending), int(done), int(failed)


def latest_result():
    with m.db() as c:
        row = c.execute(
            "SELECT result_json FROM site_tracking_jobs WHERE status='done' AND result_json IS NOT NULL ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return {}
    try:
        return json.loads(row['result_json'] or '{}')
    except Exception:
        return {}


def claim_for_site(lease_seconds=180):
    """Lease one queued website-tracking file to WordPress.

    WordPress pulls from the bot, so the unreliable bot -> Iran-host path is no
    longer used at all. Expired leases can be claimed again safely because the
    tracking plugin upserts by tracking code.
    """
    now = time.time()
    with m.db() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute(
            '''SELECT * FROM site_tracking_jobs
               WHERE status IN ('pending','retry')
                  OR (status='leased' AND next_retry<=?)
               ORDER BY created_at ASC LIMIT 1''',
            (now,),
        ).fetchone()
        if not row:
            c.commit()
            return None
        job = dict(row)
        path = Path(job['path'])
        if not path.exists():
            c.execute(
                "UPDATE site_tracking_jobs SET status='failed',last_error='queued file missing',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (job['id'],),
            )
            c.commit()
            return None
        c.execute(
            '''UPDATE site_tracking_jobs
               SET status='leased',attempts=attempts+1,next_retry=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=?''',
            (now + lease_seconds, job['id']),
        )
        c.commit()
    return _job(job['id'])


def release_job(job_id, error=''):
    with m.db() as c:
        c.execute(
            '''UPDATE site_tracking_jobs
               SET status='pending',next_retry=0,last_error=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status!='done' ''',
            (str(error)[:1200], job_id),
        )


def fail_job(job_id, error):
    with m.db() as c:
        c.execute(
            '''UPDATE site_tracking_jobs
               SET status='failed',last_error=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=?''',
            (str(error)[:1200], job_id),
        )


def complete_from_site(job_id, result):
    job = _job(job_id)
    if not job:
        return False
    if job['status'] == 'done':
        return True
    with m.db() as c:
        c.execute(
            '''UPDATE site_tracking_jobs
               SET status='done',result_json=?,last_error=NULL,next_retry=0,notified=0,updated_at=CURRENT_TIMESTAMP
               WHERE id=?''',
            (json.dumps(result or {}, ensure_ascii=False), job_id),
        )
    try:
        Path(job['path']).unlink(missing_ok=True)
    except Exception:
        pass
    return True


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


async def worker(application):
    """Only deliver completion notifications.

    Import transport is pull-only now: WordPress polls the bot. This background
    task intentionally makes zero HTTP calls to the shop.
    """
    while True:
        try:
            with m.db() as c:
                rows = c.execute(
                    "SELECT * FROM site_tracking_jobs WHERE status='done' AND notified=0 ORDER BY updated_at ASC LIMIT 10"
                ).fetchall()
            for row in rows:
                job = dict(row)
                try:
                    result = json.loads(job.get('result_json') or '{}')
                    await _notify_success(application.bot, job, result)
                    with m.db() as c:
                        c.execute('UPDATE site_tracking_jobs SET notified=1 WHERE id=?', (job['id'],))
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(5)


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
        result = latest_result()
        extra = ''
        if result:
            extra = (
                '\nآخرین ثبت سایت:\n'
                f'کل جدول: {result.get("total", 0)} | متصل: {result.get("linked", 0)} | بدون اتصال: {result.get("unlinked", 0)}'
            )
        return await update.message.reply_text(
            '📊 وضعیت رهگیری سایت\n\n'
            f'در صف برای دریافت توسط سایت: {pending}\n'
            f'ثبت موفق: {done}\n'
            f'خطای فایل محلی: {failed}'
            f'{extra}\n\n'
            '🔄 سایت فایل‌های صف را خودش از ربات دریافت می‌کند؛ ربات دیگر برای ثبت رهگیری به هاست سایت درخواست نمی‌فرستد.',
            reply_markup=st.site_tracking_menu(),
        )

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
                '''INSERT INTO site_tracking_jobs(id,user,chat,filename,path,status,next_retry,notified)
                   VALUES(?,?,?,?,?,'pending',0,0)''',
                (job_id, uid, update.effective_chat.id, filename, str(target)),
            )

        ops.reset_user_flow(uid)
        await status.edit_text(
            '✅ فایل با اطمینان روی سرور ربات ذخیره شد.\n\n'
            '🔄 سایت خودش فایل را از صف ربات دریافت و داخل جدول رهگیری وارد می‌کند. '
            'دیگر ارتباط مستقیم ربات → هاست سایت در این عملیات وجود ندارد.'
        )
        return await update.effective_chat.send_message(
            f'شناسه انتقال: {job_id[:8]}\nوقتی سایت ثبت را تأیید کند، نتیجه خودکار همینجا می‌آید.',
            reply_markup=st.site_tracking_menu(),
        )
    except Exception as exc:
        target.unlink(missing_ok=True)
        await status.edit_text(f'❌ دریافت/ذخیره فایل ناموفق بود: {exc}')
        return await update.effective_chat.send_message(
            'فایل را دوباره بفرستید یا عملیات را لغو کنید.',
            reply_markup=ops.cancel_menu(),
        )
