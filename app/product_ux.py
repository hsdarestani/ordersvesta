import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import bridge_client as bridge
from app import main as m
from app import operations as ops
from app import variations as var


# -----------------------------
# Faster signed-GET media upload
# -----------------------------
# 5 KiB keeps the final signed URL below typical 8 KiB request-line limits while
# reducing the number of round trips substantially compared with the old 3 KiB chunks.
bridge.MEDIA_CHUNK_SIZE = 5120


def fast_upload_media(self, path, filename):
    data = Path(path).read_bytes()
    if not data:
        raise RuntimeError('فایل تصویر خالی است.')

    upload_id = secrets.token_hex(16)
    digest = hashlib.sha256(data).hexdigest()
    safe_name = Path(filename or str(path)).name or 'vesta-product.jpg'

    begin = self._signed_get('media_begin', {
        'upload_id': upload_id,
        'filename': safe_name,
        'size': len(data),
        'sha256': digest,
    })
    if begin.get('already_finished') and isinstance(begin.get('result'), dict):
        return begin['result']

    chunks = []
    size = bridge.MEDIA_CHUNK_SIZE
    for offset in range(0, len(data), size):
        chunk = data[offset:offset + size]
        chunks.append((offset, bridge._b64url(chunk)))

    def send_chunk(item):
        offset, encoded = item
        return self._signed_get('media_chunk', {
            'upload_id': upload_id,
            'offset': offset,
            'data': encoded,
        })

    # Parallel requests are safe because WordPress writes each chunk at an explicit
    # offset under a file lock. Four workers noticeably reduce upload time without
    # putting excessive pressure on the host/WAF.
    workers = max(1, min(4, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(send_chunk, item) for item in chunks]
        for future in as_completed(futures):
            future.result()

    return self._signed_get('media_finish', {'upload_id': upload_id})


bridge.BridgeWooClient.upload_media = fast_upload_media
# operations.WooClient points to the same BridgeWooClient class after bridge_client import.


# -----------------------------
# Search-first category selector
# -----------------------------
def _norm(value):
    return m.norm(str(value or ''), True)


def _query_parts(text):
    raw = str(text or '').replace('\n', ',').replace('،', ',')
    return [part.strip() for part in raw.split(',') if part.strip()]


def _category_score(name, queries):
    n = _norm(name)
    if not n:
        return 0
    ntokens = set(n.split())
    best = 0
    for query in queries:
        q = _norm(query)
        if not q:
            continue
        if n == q:
            score = 1000
        elif q in n:
            score = 700 - min(100, max(0, len(n) - len(q)))
        elif n in q:
            score = 620
        else:
            qtokens = set(q.split())
            overlap = len(qtokens & ntokens)
            if not overlap:
                score = 0
            else:
                score = overlap * 120
                if qtokens and qtokens.issubset(ntokens):
                    score += 220
                score -= abs(len(ntokens) - len(qtokens)) * 5
        best = max(best, score)
    return best


def category_matches(cache, query, limit=14):
    parts = _query_parts(query)
    if not parts:
        return []
    scored = []
    for cat in cache:
        score = _category_score(cat.get('name', ''), parts)
        if score > 0:
            scored.append((score, len(str(cat.get('name', ''))), str(cat.get('name', '')), cat))
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    return [row[3] for row in scored[:limit]]


def category_search_keyboard(results, selected):
    rows = []
    selected = set(int(x) for x in selected)
    for cat in results:
        cid = int(cat['id'])
        mark = '✅' if cid in selected else '▫️'
        rows.append([
            InlineKeyboardButton(
                f'{mark} {cat["name"]}',
                callback_data=f'woo:catsearch:{cid}',
            )
        ])
    rows.append([
        InlineKeyboardButton(
            f'✅ تأیید دسته‌بندی‌ها ({len(selected)})',
            callback_data='woo:catdone',
        )
    ])
    if selected:
        rows.append([
            InlineKeyboardButton('🧹 پاک کردن انتخاب‌ها', callback_data='woo:catclear')
        ])
    return InlineKeyboardMarkup(rows)


def selected_category_names(data):
    by_id = {int(x['id']): x['name'] for x in data.get('category_cache', [])}
    return [by_id.get(int(cid), str(cid)) for cid in data.get('categories', [])]


async def send_categories(chat, uid, edit_message=None):
    state = ops.get_state(uid)
    if not state:
        return
    data = state['data']
    if not data.get('category_cache'):
        try:
            cats = await __import__('asyncio').to_thread(ops.WooClient().categories)
        except Exception as exc:
            return await chat.send_message(f'❌ دریافت دسته‌بندی‌ها ناموفق بود: {exc}')
        data['category_cache'] = [
            {'id': int(x['id']), 'name': x['name'], 'parent': int(x.get('parent') or 0)}
            for x in cats
        ]

    ops.update_state(uid, step='categories', data=data)
    chosen = selected_category_names(data)
    suffix = f'\n\nانتخاب‌شده: {"، ".join(chosen)}' if chosen else ''
    text = (
        '🔎 دسته‌بندی محصول را جستجو کنید.\n\n'
        'اسم یک یا چند دسته را تایپ کنید؛ می‌توانید با ویرگول جدا کنید.\n'
        'مثال: «رژ لب، آرایش صورت»\n\n'
        'ربات نزدیک‌ترین دسته‌بندی‌های واقعی سایت را پیشنهاد می‌دهد و می‌توانید چند مورد را انتخاب کنید.'
        + suffix
    )
    if edit_message:
        await edit_message.edit_message_text(text)
    else:
        await chat.send_message(text)


# Replace the module-global function too, so existing simple/variable flows automatically
# call the new search-first selector.
ops.send_categories = send_categories


async def text(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    state = ops.get_state(uid)
    if state and state.get('flow') == 'woo_product' and state.get('step') == 'categories':
        query = (update.message.text or '').strip()
        data = state['data']
        results = category_matches(data.get('category_cache', []), query)
        if not results:
            return await update.message.reply_text(
                'چیزی نزدیک به این عبارت پیدا نکردم. یک اسم کوتاه‌تر یا چند کلمه دیگر بنویسید؛ '
                'مثلاً «رژ»، «پوست»، «مو»، «آرایش صورت».'
            )
        data['category_query'] = query
        ops.update_state(uid, step='categories', data=data)
        chosen = selected_category_names(data)
        chosen_text = f'\nانتخاب‌شده: {"، ".join(chosen)}' if chosen else ''
        return await update.message.reply_text(
            f'پیشنهادهای نزدیک به «{query}»:{chosen_text}\n\n'
            'دسته‌های موردنظر را تیک بزنید. برای پیدا کردن دسته‌های بیشتر دوباره تایپ کنید.',
            reply_markup=category_search_keyboard(results, data.get('categories', [])),
        )
    return await var.text(update, ctx)


async def callback(update, ctx):
    q = update.callback_query
    cb = q.data or ''
    if not (cb.startswith('woo:catsearch:') or cb == 'woo:catclear'):
        return await var.callback(update, ctx)

    uid = q.from_user.id
    if not m.allowed(uid):
        return await q.answer('دسترسی ندارید', show_alert=True)
    state = ops.get_state(uid)
    if not state or state.get('flow') != 'woo_product' or state.get('step') != 'categories':
        return await q.answer('این مرحله منقضی شده است.', show_alert=True)

    await q.answer()
    data = state['data']
    selected = set(int(x) for x in data.get('categories', []))

    if cb == 'woo:catclear':
        selected.clear()
    else:
        cid = int(cb.rsplit(':', 1)[-1])
        if cid in selected:
            selected.remove(cid)
        else:
            selected.add(cid)

    data['categories'] = sorted(selected)
    ops.update_state(uid, step='categories', data=data)
    query = data.get('category_query', '')
    results = category_matches(data.get('category_cache', []), query)
    chosen = selected_category_names(data)
    chosen_text = '، '.join(chosen) if chosen else 'هیچ‌کدام'
    return await q.edit_message_text(
        f'پیشنهادهای نزدیک به «{query}»\nانتخاب‌شده: {chosen_text}\n\n'
        'برای دسته‌های دیگر کافی است دوباره اسمشان را تایپ کنید.',
        reply_markup=category_search_keyboard(results, selected),
    )
