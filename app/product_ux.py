import asyncio
import hashlib
import re
import secrets
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app import bridge_client as bridge
from app import main as m
from app import operations as ops
from app import variations as var


# ---------------------------------------------------------------------------
# Faster signed-GET media upload
# ---------------------------------------------------------------------------
# 5 KiB keeps the signed request below common 8 KiB request-line limits. Six
# parallel chunks make a single image materially faster without relying on POST
# or an external download URL (both are blocked by the Vesta host/WAF setup).
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

    workers = max(1, min(6, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(send_chunk, item) for item in chunks]
        for future in as_completed(futures):
            future.result()

    return self._signed_get('media_finish', {'upload_id': upload_id})


bridge.BridgeWooClient.upload_media = fast_upload_media


# ---------------------------------------------------------------------------
# Cover + album gallery workflow with visible progress
# ---------------------------------------------------------------------------
GALLERY_BATCHES = {}
GALLERY_DEBOUNCE_SECONDS = 1.15
GALLERY_IMAGE_CONCURRENCY = 2


def gallery_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('⏭ بدون گالری', callback_data='woo:gallery:skip')],
        [InlineKeyboardButton('❌ لغو', callback_data='woo:cancel')],
    ])


def _bar(done, total, width=10):
    total = max(1, int(total))
    done = max(0, min(int(done), total))
    filled = round(width * done / total)
    pct = round(100 * done / total)
    return f"{'█' * filled}{'░' * (width - filled)} {pct}%"


async def _safe_edit(message, text, **kwargs):
    try:
        await message.edit_text(text, **kwargs)
    except Exception:
        pass


async def _upload_telegram_photo(bot, file_id, index=0):
    tg_file = await bot.get_file(file_id, read_timeout=60, connect_timeout=30)
    tmp = Path(tempfile.gettempdir()) / f'woo_{uuid.uuid4().hex}_{index}.jpg'
    try:
        await tg_file.download_to_drive(custom_path=str(tmp), read_timeout=120, connect_timeout=30)
        media = await asyncio.to_thread(ops.WooClient().upload_media, tmp, tmp.name)
        return {
            'id': int(media['id']),
            'src': media.get('source_url', ''),
        }
    finally:
        tmp.unlink(missing_ok=True)


async def _after_gallery(chat, uid, data):
    cover = data.get('cover')
    gallery = list(data.get('gallery') or [])
    data['images'] = ([cover] if cover else []) + gallery

    if data.get('type') == 'variable':
        ops.update_state(uid, step='var_attribute_name', data=data)
        await chat.send_message(
            f'✅ تصاویر کامل شد.\nکاور: 1\nگالری: {len(gallery)}\n\n'
            '🎛 محصول متغیر است. نام ویژگی Variation را بفرستید؛ مثلاً:\nرنگ\nحجم\nمدل'
        )
    else:
        ops.update_state(uid, step='stock', data=data)
        await chat.send_message(
            f'✅ تصاویر کامل شد.\nکاور: 1\nگالری: {len(gallery)}\n\n'
            '📦 موجودی محصول چند عدد است؟'
        )


async def _process_gallery_batch(uid, chat_id, group_id, bot):
    try:
        await asyncio.sleep(GALLERY_DEBOUNCE_SECONDS)
        batch = GALLERY_BATCHES.get(uid)
        if not batch or batch.get('group_id') != group_id:
            return
        file_ids = list(batch.get('file_ids') or [])
        GALLERY_BATCHES.pop(uid, None)

        state = ops.get_state(uid)
        if not state or state.get('flow') != 'woo_product' or state.get('step') != 'gallery':
            return
        data = state['data']
        if not file_ids:
            return

        ops.update_state(uid, step='gallery_uploading', data=data)
        progress = await bot.send_message(
            chat_id,
            f'📤 در حال آپلود گالری…\n0/{len(file_ids)} عکس\n{_bar(0, len(file_ids))}'
        )

        sem = asyncio.Semaphore(GALLERY_IMAGE_CONCURRENCY)

        async def worker(idx, fid):
            async with sem:
                media = await _upload_telegram_photo(bot, fid, idx)
                return idx, media

        tasks = [asyncio.create_task(worker(i, fid)) for i, fid in enumerate(file_ids)]
        results = {}
        failures = []
        completed = 0
        for future in asyncio.as_completed(tasks):
            try:
                idx, media = await future
                results[idx] = media
            except Exception as exc:
                failures.append(str(exc))
            completed += 1
            await _safe_edit(
                progress,
                f'📤 در حال آپلود گالری…\n{completed}/{len(file_ids)} عکس\n{_bar(completed, len(file_ids))}'
            )

        uploaded = [results[i] for i in sorted(results)]
        data.setdefault('gallery', []).extend(uploaded)
        data['images'] = ([data['cover']] if data.get('cover') else []) + list(data.get('gallery') or [])

        if failures:
            ops.update_state(uid, step='gallery', data=data)
            await _safe_edit(
                progress,
                f'⚠️ آپلود گالری کامل نشد.\n'
                f'موفق: {len(uploaded)}/{len(file_ids)}\n{_bar(len(uploaded), len(file_ids))}\n\n'
                'عکس‌های ناموفق را دوباره یکجا بفرستید.'
            )
            return

        await _safe_edit(
            progress,
            f'✅ گالری آپلود شد.\n{len(uploaded)}/{len(file_ids)} عکس\n{_bar(len(file_ids), len(file_ids))}'
        )
        await _after_gallery(await bot.get_chat(chat_id), uid, data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state = ops.get_state(uid)
        if state and state.get('flow') == 'woo_product':
            ops.update_state(uid, step='gallery', data=state['data'])
        try:
            await bot.send_message(chat_id, f'❌ آپلود گالری ناموفق بود: {exc}\nدوباره آلبوم را بفرستید.')
        except Exception:
            pass


async def photo(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    state = ops.get_state(uid)
    if not state or state.get('flow') != 'woo_product':
        return

    step = state.get('step')
    data = state['data']

    if step == 'cover':
        if update.message.media_group_id:
            return await update.message.reply_text('کاور باید فقط یک عکس باشد. لطفاً یک عکس جداگانه بفرستید.')
        status = await update.message.reply_text('📤 آپلود کاور…\n░░░░░░░░░░ 10%')
        try:
            p = update.message.photo[-1]
            tg_file = await p.get_file(read_timeout=60, connect_timeout=30)
            await _safe_edit(status, '📤 آپلود کاور…\n███░░░░░░░ 30%')
            tmp = Path(tempfile.gettempdir()) / f'woo_cover_{uuid.uuid4().hex}.jpg'
            try:
                await tg_file.download_to_drive(custom_path=str(tmp), read_timeout=120, connect_timeout=30)
                await _safe_edit(status, '📤 آپلود کاور…\n█████░░░░░ 50%')
                media = await asyncio.to_thread(ops.WooClient().upload_media, tmp, tmp.name)
            finally:
                tmp.unlink(missing_ok=True)

            cover = {'id': int(media['id']), 'src': media.get('source_url', '')}
            data['cover'] = cover
            data['gallery'] = []
            data['images'] = [cover]
            ops.update_state(uid, step='gallery', data=data)
            await _safe_edit(status, '✅ کاور آپلود شد.\n██████████ 100%')
            return await update.message.reply_text(
                '🖼 حالا عکس‌های گالری را **یکجا به صورت آلبوم** بفرستید.\n\n'
                'همه عکس‌ها را در تلگرام با هم انتخاب و Send کنید؛ ربات بعد از دریافت کل آلبوم '
                'آن‌ها را موازی آپلود می‌کند و Progress را نشان می‌دهد.\n\n'
                'اگر گالری ندارید «بدون گالری» را بزنید.',
                reply_markup=gallery_keyboard(),
                parse_mode='Markdown',
            )
        except Exception as exc:
            await _safe_edit(status, f'❌ آپلود کاور ناموفق بود: {exc}')
            return

    if step == 'gallery':
        file_id = update.message.photo[-1].file_id
        group_id = str(update.message.media_group_id or f'single-{update.message.message_id}')
        batch = GALLERY_BATCHES.get(uid)

        if batch and batch.get('group_id') != group_id:
            return await update.message.reply_text('⏳ یک آلبوم در حال دریافت است؛ چند لحظه صبر کنید.')

        if not batch:
            batch = {'group_id': group_id, 'file_ids': [], 'task': None}
            GALLERY_BATCHES[uid] = batch

        if file_id not in batch['file_ids']:
            batch['file_ids'].append(file_id)

        old_task = batch.get('task')
        if old_task and not old_task.done():
            old_task.cancel()
        batch['task'] = asyncio.create_task(
            _process_gallery_batch(uid, update.effective_chat.id, group_id, ctx.bot)
        )
        return

    if step == 'gallery_uploading':
        return await update.message.reply_text('⏳ گالری در حال آپلود است؛ Progress را بالا می‌بینید.')


# ---------------------------------------------------------------------------
# Short description + weight
# ---------------------------------------------------------------------------
async def _ask_short_description(chat, uid, data, intro=None):
    ops.update_state(uid, step='short_description', data=data)
    text = '✍️ کپشن / توضیح کوتاه محصول را بفرستید.\nاین متن در فیلد «توضیح کوتاه محصول» ووکامرس ذخیره می‌شود.'
    if intro:
        text = intro + '\n\n' + text
    await chat.send_message(text)


async def finish_variations_to_details(chat, uid, data):
    data['stock'] = sum(int(v.get('stock', 0)) for v in data.get('variations', []))
    await _ask_short_description(
        chat,
        uid,
        data,
        '✅ Variationها کامل شدند:\n\n' + var.variations_summary(data),
    )


# var.text looks this name up from the module globals at runtime.
var.finish_variations_to_categories = finish_variations_to_details


def _parse_weight(text):
    value = str(text or '').translate(m.DIGITS).strip().replace(' ', '')
    if value.count(',') == 1 and '.' not in value:
        value = value.replace(',', '.')
    value = value.replace(',', '')
    if not re.fullmatch(r'\d+(?:\.\d+)?', value):
        return None
    try:
        if float(value) <= 0:
            return None
    except Exception:
        return None
    return value


# ---------------------------------------------------------------------------
# Search-first category selector
# ---------------------------------------------------------------------------
def _norm(value):
    # Keep word boundaries.  The old implementation used compact=True, which
    # turned e.g. "مراقبت پوست" into one token ("مراقبتپوست").  As a result,
    # the token-overlap fallback below could never match natural multi-word
    # searches unless the complete phrase happened to be a literal substring.
    return m.norm(str(value or ''), False)


CATEGORY_PHRASE_ALIASES = (
    (re.compile(r'\bپاک\s*کننده(?:\s*ها)?\b'), 'شوینده'),
    (re.compile(r'\bفیس\s*واش\b'), 'شوینده'),
    (re.compile(r'\bاسکین\s*کر\b'), 'مراقبت پوست'),
)

CATEGORY_TOKEN_ALIASES = {
    'شستشو': 'شوینده',
    'شستشوی': 'شوینده',
    'پاککننده': 'شوینده',
    'کلینزر': 'شوینده',
    'پوستی': 'پوست',
    'مراقبتی': 'مراقبت',
    'آرایشی': 'آرایش',
    'بهداشتی': 'بهداشت',
    'موها': 'مو',
}

CATEGORY_STOPWORDS = {
    'از', 'با', 'برای', 'به', 'بندی', 'در', 'دسته', 'محصول', 'محصولات',
    'لوازم', 'انواع', 'و',
}


def _canonical_category_text(value):
    text = _norm(value)
    for pattern, replacement in CATEGORY_PHRASE_ALIASES:
        text = pattern.sub(replacement, text)
    tokens = [CATEGORY_TOKEN_ALIASES.get(token, token) for token in text.split()]
    return ' '.join(tokens)


def _meaningful_words(value):
    return [
        token for token in _canonical_category_text(value).split()
        if token and token not in CATEGORY_STOPWORDS
    ]


def _meaningful_tokens(value):
    return set(_meaningful_words(value))


def _query_parts(text):
    raw = str(text or '').replace('\n', ',').replace('،', ',')
    return [part.strip() for part in raw.split(',') if part.strip()]


def _category_score(name, queries):
    n = _canonical_category_text(name)
    if not n:
        return 0
    nwords = _meaningful_words(n)
    ncompact = ''.join(nwords)
    ntokens = set(nwords)
    scores = []
    for query in queries:
        q = _canonical_category_text(query)
        if not q:
            continue
        qwords = _meaningful_words(q)
        qcompact = ''.join(qwords)
        qtokens = set(qwords)
        if ncompact == qcompact:
            score = 1200 + min(120, max(0, len(nwords) - 1) * 80)
        elif qcompact in ncompact:
            score = 900 - min(120, max(0, len(ncompact) - len(qcompact)))
        elif ncompact in qcompact:
            # Prefer the most specific category contained in a longer natural
            # query: "شوینده صورت" should rank above the generic "صورت".
            score = 820 + min(240, max(0, len(nwords) - 1) * 120)
        else:
            overlap = len(qtokens & ntokens)
            if overlap:
                query_coverage = overlap / max(1, len(qtokens))
                category_coverage = overlap / max(1, len(ntokens))
                score = round(
                    (query_coverage * 360)
                    + (category_coverage * 300)
                    + (overlap * 90)
                )
            else:
                # Typo tolerance is deliberately token-based and conservative;
                # it catches inputs such as "شویننده" without suggesting an
                # unrelated category merely because a short substring matches.
                similarity = max(
                    (
                        SequenceMatcher(None, qtoken, ntoken).ratio()
                        for qtoken in qtokens if len(qtoken) >= 3
                        for ntoken in ntokens if len(ntoken) >= 3
                    ),
                    default=0,
                )
                score = round(similarity * 420) if similarity >= 0.72 else 0
        if score > 0:
            scores.append(score)

    if not scores:
        return 0
    scores.sort(reverse=True)
    # Reward a category that matches several requested terms (for example both
    # "شوینده" and "صورت") while keeping the best individual match dominant.
    return scores[0] + round(sum(scores[1:]) * 0.45)


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
            InlineKeyboardButton(f'{mark} {cat["name"]}', callback_data=f'woo:catsearch:{cid}')
        ])
    rows.append([
        InlineKeyboardButton(f'✅ تأیید دسته‌بندی‌ها ({len(selected)})', callback_data='woo:catdone')
    ])
    if selected:
        rows.append([InlineKeyboardButton('🧹 پاک کردن انتخاب‌ها', callback_data='woo:catclear')])
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
            cats = await asyncio.to_thread(ops.WooClient().categories)
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
        'اسم یک یا چند دسته را تایپ کنید؛ با ویرگول جدا کنید.\n'
        'مثال: «رژ لب، آرایش صورت»\n\n'
        'ربات نزدیک‌ترین دسته‌بندی‌های واقعی سایت را پیشنهاد می‌دهد و می‌توانید چند مورد را انتخاب کنید.'
        + suffix
    )
    if edit_message:
        await edit_message.edit_message_text(text)
    else:
        await chat.send_message(text)


ops.send_categories = send_categories


# ---------------------------------------------------------------------------
# Better preview + publishing with caption/weight
# ---------------------------------------------------------------------------
async def product_preview(chat, uid):
    state = ops.get_state(uid)
    if not state:
        return
    data = state['data']
    cats = {int(x['id']): x['name'] for x in data.get('category_cache', [])}
    selected = [cats.get(int(i), str(i)) for i in data.get('categories', [])]
    gallery_count = len(data.get('gallery') or [])
    caption = str(data.get('short_description') or '')
    if len(caption) > 220:
        caption = caption[:217] + '…'

    if data.get('type') == 'variable':
        pricing = (
            f'🎛 ویژگی: {data.get("attribute_name", "—")}\n'
            f'{var.variations_summary(data)}'
        )
        typ = 'متغیر'
    else:
        pricing = (
            f'موجودی: {data.get("stock", 0)}\n'
            f'قیمت اصلی: {data.get("regular_price", "—")}\n'
            f'قیمت تخفیف: {data.get("sale_price") or "—"}'
        )
        typ = 'ثابت'

    text = (
        '🧾 پیش‌نمایش محصول\n\n'
        f'نام: {data.get("name")}\n'
        f'نوع: {typ}\n'
        f'{pricing}\n'
        f'وزن: {data.get("weight", "—")}\n'
        f'کاور: {"✅" if data.get("cover") else "❌"}\n'
        f'گالری: {gallery_count} عکس\n'
        f'دسته‌بندی: {"، ".join(selected) if selected else "بدون دسته"}\n\n'
        f'کپشن:\n{caption}\n\n'
        'اگر اطلاعات درست است ثبت نهایی را بزنید.'
    )
    await chat.send_message(text, reply_markup=ops.preview_keyboard())


ops.product_preview = product_preview


async def publish_simple_with_details(chat, uid):
    state = ops.get_state(uid)
    data = state['data']
    payload = {
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
    product = await asyncio.to_thread(ops.WooClient().create_product, payload)
    ops.clear_state(uid)
    await chat.send_message(
        f'✅ محصول ثبت شد.\n\n#{product.get("id")} — {product.get("name")}\n{product.get("permalink", "")}',
        reply_markup=ops.woo_menu(),
    )
    return product


async def publish_variable_with_details(chat, uid, data):
    client = ops.WooClient()
    attr_name = data.get('attribute_name') or 'گزینه'
    values = [v['option'] for v in data.get('variations', [])]
    parent_payload = {
        'name': data['name'],
        'type': 'variable',
        'status': 'publish',
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


# var.publish_product resolves these module globals dynamically.
var.ORIG_PUBLISH = publish_simple_with_details
var.publish_variable = publish_variable_with_details


# ---------------------------------------------------------------------------
# Top-level text/callback routers
# ---------------------------------------------------------------------------
async def text(update, ctx):
    if not await m.access(update):
        return
    uid = update.effective_user.id
    state = ops.get_state(uid)

    if state and state.get('flow') == 'woo_product':
        step = state.get('step')
        data = state['data']
        value = (update.message.text or '').strip()

        # Numeric simple-product sale price: continue to caption instead of jumping
        # straight to categories as the legacy flow did.
        if step == 'sale_price' and data.get('type') != 'variable':
            x = value.translate(m.DIGITS).replace(',', '').strip()
            if not x.isdigit():
                return await update.message.reply_text('قیمت تخفیف را فقط به صورت عدد وارد کنید.')
            if int(x) >= int(data.get('regular_price') or 0):
                return await update.message.reply_text('قیمت تخفیف باید از قیمت اصلی کمتر باشد.')
            data['sale_price'] = x
            return await _ask_short_description(update.effective_chat, uid, data)

        if step == 'short_description':
            if not value:
                return await update.message.reply_text('کپشن نمی‌تواند خالی باشد؛ توضیح کوتاه محصول را بفرستید.')
            data['short_description'] = value
            ops.update_state(uid, step='weight', data=data)
            return await update.message.reply_text(
                '⚖️ وزن محصول را عددی وارد کنید.\n'
                'عدد باید مطابق واحد وزن تنظیم‌شده در ووکامرس سایت باشد؛ مثال: 0.25 یا 250.'
            )

        if step == 'weight':
            weight = _parse_weight(value)
            if weight is None:
                return await update.message.reply_text('وزن را فقط به صورت عدد مثبت وارد کنید؛ مثال: 0.25 یا 250.')
            data['weight'] = weight
            ops.update_state(uid, step='categories', data=data)
            return await send_categories(update.effective_chat, uid)

        if step == 'categories':
            query = value
            results = category_matches(data.get('category_cache', []), query)
            if not results:
                return await update.message.reply_text(
                    'چیزی نزدیک به این عبارت پیدا نکردم. کوتاه‌تر بنویسید؛ '
                    'مثلاً «رژ»، «پوست»، «مو»، «آرایش صورت».'
                )
            data['category_query'] = query
            ops.update_state(uid, step='categories', data=data)
            chosen = selected_category_names(data)
            chosen_text = f'\nانتخاب‌شده: {"، ".join(chosen)}' if chosen else ''
            return await update.message.reply_text(
                f'پیشنهادهای نزدیک به «{query}»:{chosen_text}\n\n'
                'دسته‌های موردنظر را تیک بزنید. برای دسته‌های بیشتر دوباره تایپ کنید.',
                reply_markup=category_search_keyboard(results, data.get('categories', [])),
            )

    return await var.text(update, ctx)


async def callback(update, ctx):
    q = update.callback_query
    cb = q.data or ''
    uid = q.from_user.id
    state = ops.get_state(uid)

    if cb.startswith('woo:type:') and state and state.get('flow') == 'woo_product':
        if not m.allowed(uid):
            return await q.answer('دسترسی ندارید', show_alert=True)
        await q.answer()
        typ = cb.split(':', 2)[-1]
        data = state['data']
        data['type'] = typ
        data['cover'] = None
        data['gallery'] = []
        data['images'] = []
        ops.update_state(uid, step='cover', data=data)
        await q.edit_message_text('✅ نوع محصول: ' + ('متغیر' if typ == 'variable' else 'ثابت'))
        return await q.message.reply_text(
            '🖼 اول **فقط عکس اصلی / کاور محصول** را بفرستید.\n'
            'گالری را در مرحله بعد جدا و یکجا می‌گیریم.',
            parse_mode='Markdown',
        )

    if cb == 'woo:gallery:skip' and state and state.get('flow') == 'woo_product' and state.get('step') == 'gallery':
        if not m.allowed(uid):
            return await q.answer('دسترسی ندارید', show_alert=True)
        await q.answer()
        data = state['data']
        data['gallery'] = []
        data['images'] = [data['cover']] if data.get('cover') else []
        await q.edit_message_text('⏭ محصول بدون گالری ادامه پیدا می‌کند.')
        return await _after_gallery(q.message.chat, uid, data)

    if cb == 'woo:sale:skip' and state and state.get('flow') == 'woo_product' and state.get('data', {}).get('type') != 'variable':
        if not m.allowed(uid):
            return await q.answer('دسترسی ندارید', show_alert=True)
        await q.answer()
        data = state['data']
        data['sale_price'] = ''
        await q.edit_message_text('بدون تخفیف ثبت شد.')
        return await _ask_short_description(q.message.chat, uid, data)

    if cb.startswith('woo:catsearch:') or cb == 'woo:catclear':
        if not m.allowed(uid):
            return await q.answer('دسترسی ندارید', show_alert=True)
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
            'برای دسته‌های دیگر دوباره اسمشان را تایپ کنید.',
            reply_markup=category_search_keyboard(results, selected),
        )

    return await var.callback(update, ctx)
