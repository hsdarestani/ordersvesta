import os

import httpx


DEFAULT_URL = 'https://rest.payamak-panel.com/api/SendSMS/SendSMS'


def send(phone, text):
    username = os.getenv('MELLIPAYAMAK_USERNAME', '').strip()
    api_key = os.getenv('MELLIPAYAMAKAPIKEY', '').strip()
    sender = os.getenv('MELLIPAYAMAK_FROM', '').strip()
    url = os.getenv('MELLIPAYAMAK_URL', DEFAULT_URL).strip() or DEFAULT_URL

    if not username:
        raise RuntimeError('MELLIPAYAMAK_USERNAME تنظیم نشده است.')
    if not api_key:
        raise RuntimeError('MELLIPAYAMAKAPIKEY تنظیم نشده است.')
    if not sender:
        raise RuntimeError('MELLIPAYAMAK_FROM تنظیم نشده است.')

    payload = {
        'username': username,
        'password': api_key,
        'to': phone,
        'from': sender,
        'text': text,
        'isFlash': 'false',
    }

    # Melipayamak's REST Web Service expects form-urlencoded data. The API key
    # is sent in the password field, as documented by the provider panel.
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
