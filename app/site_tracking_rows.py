import asyncio
import csv
import json
import os
import secrets
import time
import zlib
from pathlib import Path

from app import bridge_client as bridge
from app import main as m
from app import operations as ops
from app import site_tracking as st

# Keep each signed URL comfortably below common 8 KiB request-line limits.
MAX_PACKED_PAYLOAD = 4700
MAX_ROWS_PER_BATCH = 80


def _trim_row(values):
    row = [str(v or '').strip() for v in values]
    while row and row[-1] == '':
        row.pop()
    return row


def _xlsx_rows(path):
    sparse = m.ooxml_rows(path)
    dense = []
    for row in sparse:
        if not row:
            continue
        last = max(row.keys()) if row else -1
        values = [row.get(i, '') for i in range(last + 1)]
        values = _trim_row(values)
        if any(values):
            dense.append(values)
    return dense


def _csv_rows(path):
    raw = Path(path).read_bytes()
    text = raw.decode('utf-8-sig', errors='replace')
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
        delimiter = dialect.delimiter
    except Exception:
        first = text.splitlines()[0] if text.splitlines() else ''
        counts = {d: first.count(d) for d in (',', ';', '\t')}
        delimiter = max(counts, key=counts.get) if counts else ','
    rows = []
    for row in csv.reader(text.splitlines(), delimiter=delimiter):
        clean = _trim_row(row)
        if any(clean):
            rows.append(clean)
    return rows


def read_tracking_rows(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {'.xlsx', '.xlsm'}:
        rows = _xlsx_rows(path)
    elif suffix == '.csv':
        rows = _csv_rows(path)
    else:
        raise RuntimeError('فقط XLSX / XLSM / CSV پشتیبانی می‌شود.')
    if len(rows) < 2:
        raise RuntimeError('ردیف قابل استفاده‌ای در فایل پیدا نشد.')
    return rows


def _packed_len(payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    return len(bridge._b64url(zlib.compress(raw, 9)))


def make_batches(rows, filename, import_id):
    header = rows[0]
    data_rows = rows[1:]
    batches = []
    current = []

    def payload_for(candidate, index):
        return {
            'import_id': import_id,
            'batch_index': index,
            'filename': filename,
            'auto_match': True,
            'rows': [header] + candidate,
        }

    for row in data_rows:
        candidate = current + [row]
        tentative = payload_for(candidate, len(batches))
        if current and (
            len(candidate) > MAX_ROWS_PER_BATCH
            or _packed_len(tentative) > MAX_PACKED_PAYLOAD
        ):
            batches.append(current)
            current = [row]
        else:
            current = candidate
    if current:
        batches.append(current)
    return header, batches


def _tracking_call(client, op, payload):
    """Long-running but background-only Bridge call.

    Product/menu handlers use short timeouts for responsiveness. Tracking imports
    are already persisted in a durable queue, so they can afford longer requests
    and retries without freezing Telegram.
    """
    errors = []
    relay = (os.getenv('BRIDGE_RELAY_URL') or '').rstrip('/')
    endpoints = []
    if relay and relay != client.url:
        endpoints.append(('cloudflare-relay', f'{relay}/'))
    endpoints.extend([
        ('admin-ajax', f'{client.url}/wp-admin/admin-ajax.php'),
        ('home', f'{client.url}/'),
    ])

    for round_no in range(2):
        for label, endpoint in endpoints:
            params = client._signed_params(op, payload)
            try:
                response = client._stdlib_get(endpoint, params, 28.0)
                return client._decode(response)
            except RuntimeError as exc:
                # Authentication / application errors are deterministic. Do not
                # fan them out across every route unless it is a transient 5xx-like
                # message coming from a proxy.
                text = str(exc)
                if '401' in text or '403' in text or 'Invalid bridge' in text or 'Unknown operation' in text:
                    raise
                errors.append(f'{label}: {exc}')
            except Exception as exc:
                errors.append(f'{label}: {exc}')
        if round_no == 0:
            time.sleep(3.0)
    raise RuntimeError('انتقال رهگیری به سایت ناموفق بود: ' + ' | '.join(errors[-4:]))


async def upload_tracking_rows(path, filename, progress_message=None):
    rows = await asyncio.to_thread(read_tracking_rows, path)
    import_id = secrets.token_hex(16)
    header, batches = make_batches(rows, filename, import_id)
    if not batches:
        raise RuntimeError('ردیف قابل انتقالی در فایل پیدا نشد.')

    client = ops.WooClient()
    totals = {
        'filename': filename,
        'rows': len(rows) - 1,
        'inserted': 0,
        'updated': 0,
        'skipped': 0,
        'linked': 0,
        'unlinked': 0,
        'total': 0,
    }

    for index, batch in enumerate(batches):
        payload = {
            'import_id': import_id,
            'batch_index': index,
            'filename': filename,
            'auto_match': True,
            'rows': [header] + batch,
        }
        result = await asyncio.to_thread(_tracking_call, client, 'vpt_import_rows_batch', payload)
        for key in ('inserted', 'updated', 'skipped', 'linked', 'unlinked'):
            totals[key] += int(result.get(key, 0) or 0)
        totals['total'] = int(result.get('total', totals['total']) or totals['total'])

        if progress_message:
            try:
                done = index + 1
                pct = 15 + round(80 * done / len(batches))
                await progress_message.edit_text(
                    '📮 در حال ثبت مستقیم ردیف‌ها در جدول رهگیری سایت…\n'
                    f'بخش {done}/{len(batches)}\n'
                    f'{st._progress_bar(pct)}'
                )
            except Exception:
                pass

        # Avoid WAF burst detection while keeping the transfer reasonably fast.
        if index + 1 < len(batches):
            await asyncio.sleep(0.7)

    return totals


# The durable queue calls st.upload_tracking_file dynamically, so replacing it
# here upgrades queued and future jobs without changing queue semantics.
st.upload_tracking_file = upload_tracking_rows
