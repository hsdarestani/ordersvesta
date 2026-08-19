"""Compatibility fixes for WooCommerce authentication on Vesta hosting.

The Vesta web server accepts WooCommerce REST credentials in the query string but can
reject requests carrying HTTP Basic Authorization before WordPress sees them.  Keep
all WooCommerce calls on the query-auth path so GET/POST requests reach wc/v3.
"""

from app import operations as ops


def query_auth_wc(self, method, path, **kwargs):
    url = f'{self.url}/wp-json/wc/v3/{path.lstrip("/")}'
    params = dict(kwargs.pop('params', {}) or {})
    params['consumer_key'] = self.ck
    params['consumer_secret'] = self.cs

    r = self.c.request(method, url, params=params, **kwargs)
    if r.status_code >= 400:
        content_type = (r.headers.get('content-type') or '').lower()
        # Never include the request URL here because it contains the WooCommerce secret.
        if r.status_code == 403 and 'application/json' not in content_type:
            raise RuntimeError(
                'WooCommerce HTTP 403 توسط وب‌سرور رد شد؛ درخواست Query Auth هم به WordPress نرسید.'
            )
        try:
            body = r.json()
            code = body.get('code') if isinstance(body, dict) else None
            message = body.get('message') if isinstance(body, dict) else None
            detail = ' | '.join(x for x in (str(code or ''), str(message or '')) if x)
            if not detail:
                detail = str(body)[:350]
        except Exception:
            detail = r.text[:350]
        raise RuntimeError(f'WooCommerce HTTP {r.status_code}: {detail}')
    return r


# Patch the client used by every WooCommerce operation in the Telegram bot.
ops.WooClient.wc = query_auth_wc
