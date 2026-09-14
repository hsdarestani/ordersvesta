import os
import re

import httpx


DEFAULT_URL = 'https://rest.payamak-panel.com/api/SendSMS/BaseServiceNumber'
DEFAULT_BODY_ID = '537070'


def _pattern_value(text):
    """Return the variable value expected by the approved tracking pattern.

    Existing bot call-sites still pass the rendered human-readable SMS text. The
    shared-service endpoint must receive only the value for the pattern's single
    variable (the tracking code), so extract it without changing every caller.
    """
    value = str(text or '').strip()
    match = re.search(r'کد\s*رهگیری\s*مرسوله\s*:\s*([^\r\n]+)', value)
    if match:
        return match.group(1).strip()
    return value


def send(phone, text):
    username = os.getenv('MELLIPAYAMAK_USERNAME', '').strip()
    api_key = os.getenv('MELLIPAYAMAKAPIKEY', '').strip()
    body_id = os.getenv('MELLIPAYAMAK_BODY_ID', DEFAULT_BODY_ID).strip() or DEFAULT_BODY_ID
    url = os.getenv('MELLIPAYAMAK_URL', DEFAULT_URL).strip() or DEFAULT_URL
    pattern_value = _pattern_value(text)

    if not username:
        raise RuntimeError('MELLIPAYAMAK_USERNAME تنظیم نشده است.')
    if not api_key:
        raise RuntimeError('MELLIPAYAMAKAPIKEY تنظیم نشده است.')
    if not body_id.isdigit():
        raise RuntimeError('MELLIPAYAMAK_BODY_ID باید عددی باشد.')
    if not pattern_value:
        raise RuntimeError('کد رهگیری برای پیامک الگو خالی است.')

    payload = {
        'username': username,
        # Melipayamak explicitly allows APIKey to be supplied in Password.
        'password': api_key,
        'text': pattern_value,
        'to': str(phone),
        'bodyId': body_id,
    }

    # Shared-service/pattern API. No dedicated sender number is required here;
    # Melipayamak sends the approved template using its service line.
    response = httpx.post(url, data=payload, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f'ملی پیامک HTTP {response.status_code}: {response.text[:300]}')

    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(f'پاسخ نامعتبر از ملی پیامک: {response.text[:300]}') from exc

    if not isinstance(data, dict):
        raise RuntimeError(f'پاسخ نامعتبر از ملی پیامک: {data!r}')

    try:
        status = int(data.get('RetStatus', 0))
    except (TypeError, ValueError):
        status = 0

    if status != 1:
        detail = data.get('StrRetStatus') or data.get('Value') or str(data)
        raise RuntimeError(f'ملی پیامک: {detail}')

    return data
