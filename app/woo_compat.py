"""Robust WooCommerce authentication for the Vesta host.

The host/WAF returns a generic HTML 403 when the Woo consumer secret is placed in
query parameters, and it may also interfere with Basic Auth using ck_/cs_.

WooCommerce is integrated with the WordPress REST API, so a WordPress Application
Password is a valid REST authentication method for a WordPress user with the required
WooCommerce capabilities.  We therefore try the already configured WordPress
username/Application Password first.  If the web server strips Basic Authorization,
we fall back to WooCommerce OAuth 1.0a query authentication.  OAuth sends the consumer
key and a signature, but never sends the consumer secret itself in the URL.
"""

import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

from app import operations as ops


def _json_error(response):
    try:
        body = response.json()
        if isinstance(body, dict):
            code = str(body.get('code') or '')
            message = str(body.get('message') or '')
            detail = ' | '.join(x for x in (code, message) if x)
            return detail or str(body)[:300]
        return str(body)[:300]
    except Exception:
        return response.text[:300]


def _is_html_block(response):
    ctype = (response.headers.get('content-type') or '').lower()
    body = (response.text or '')[:200].lower()
    return response.status_code in (401, 403) and (
        'text/html' in ctype or '<html' in body or '<!doctype html' in body
    )


def _pct(value):
    # RFC 3986 / OAuth percent encoding.
    return quote(str(value), safe='~-._')


def _oauth_params(client, method, url, params):
    """Build WooCommerce one-legged OAuth 1.0a query params using HMAC-SHA256."""
    signed = dict(params or {})
    signed.update({
        'oauth_consumer_key': client.ck,
        'oauth_nonce': secrets.token_hex(16),
        'oauth_signature_method': 'HMAC-SHA256',
        'oauth_timestamp': str(int(time.time())),
    })

    pairs = []
    for key, value in signed.items():
        if isinstance(value, (list, tuple)):
            values = value
        else:
            values = [value]
        for item in values:
            pairs.append((_pct(key), _pct(item)))
    pairs.sort(key=lambda item: (item[0], item[1]))
    normalized = '&'.join(f'{k}={v}' for k, v in pairs)
    base_string = '&'.join((_pct(method.upper()), _pct(url), _pct(normalized)))

    # OAuth signing key is consumer_secret + '&' (no token secret for one-legged auth).
    signing_key = f'{_pct(client.cs)}&'.encode()
    digest = hmac.new(signing_key, base_string.encode(), hashlib.sha256).digest()
    signed['oauth_signature'] = base64.b64encode(digest).decode()
    return signed


def smart_wc(self, method, path, **kwargs):
    url = f'{self.url}/wp-json/wc/v3/{path.lstrip("/")}'
    params = dict(kwargs.pop('params', {}) or {})
    attempts = []

    # 1) Preferred on this host: WordPress Application Password. WooCommerce REST
    # endpoints can use WordPress REST authentication methods. The WP user must have
    # the WooCommerce capabilities needed by the requested endpoint.
    if self.wp_user and self.wp_app_password:
        passwords = [self.wp_app_password]
        compact = self.wp_app_password.replace(' ', '')
        if compact != self.wp_app_password:
            passwords.append(compact)

        for app_password in passwords:
            try:
                response = self.c.request(
                    method,
                    url,
                    auth=(self.wp_user, app_password),
                    params=params,
                    **kwargs,
                )
            except Exception as exc:
                attempts.append(f'WP-App: network {type(exc).__name__}')
                continue

            if response.status_code < 400:
                return response
            if _is_html_block(response):
                attempts.append(f'WP-App: webserver {response.status_code}')
                # Trying the same Authorization header with whitespace removed is only
                # useful for a WordPress JSON auth failure, not a pre-WordPress WAF block.
                break
            attempts.append(f'WP-App: {response.status_code} {_json_error(response)}')

    # 2) No Authorization header and no raw consumer_secret in the URL. This avoids
    # both classes of WAF rule that have already been observed on the Vesta host.
    try:
        oauth = _oauth_params(self, method, url, params)
        response = self.c.request(method, url, params=oauth, **kwargs)
        if response.status_code < 400:
            return response
        if _is_html_block(response):
            attempts.append(f'OAuth: webserver {response.status_code}')
        else:
            attempts.append(f'OAuth: {response.status_code} {_json_error(response)}')
    except Exception as exc:
        attempts.append(f'OAuth: network {type(exc).__name__}')

    # Do not put ck/cs or request URLs in errors/logs. They contain credentials.
    detail = ' ; '.join(attempts[-4:]) or 'unknown authentication error'
    raise RuntimeError(
        'اتصال WooCommerce برقرار نشد. مسیر Application Password و OAuth هر دو تست شدند. '
        f'جزئیات امن: {detail}'
    )


# Patch every WooCommerce call used by product/category/variation operations.
ops.WooClient.wc = smart_wc
