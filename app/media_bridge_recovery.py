"""Reliable transport policy for WordPress media uploads.

Normal Bridge reads/writes keep using the existing transport. Media chunks are special:
large signed query strings are more likely to be delayed by the Cloudflare Worker relay,
while production diagnostics show the Vesta host itself is reachable quickly from the VPS.
Prefer WordPress directly for media traffic, keep the relay as a fallback for begin/chunk,
and never fan out a timed-out media_finish because that could create a duplicate attachment.
"""
import os

import httpx

from app import bridge_client as bridge

_ORIGINAL_SIGNED_GET = bridge.BridgeWooClient._signed_get
_MEDIA_OPS = {'media_begin', 'media_chunk', 'media_finish'}


def media_endpoints(client):
    url = str(getattr(client, 'url', '') or '').rstrip('/')
    if not url:
        return ()

    endpoints = [
        ('admin-ajax', f'{url}/wp-admin/admin-ajax.php'),
        ('home', f'{url}/'),
    ]

    relay = (os.getenv('BRIDGE_RELAY_URL') or '').rstrip('/')
    if relay and relay != url:
        endpoints.append(('cloudflare-relay', f'{relay}/'))
    return tuple(endpoints)


def _retryable_runtime_error(exc):
    text = str(exc).lower()
    return (
        'http 408' in text
        or 'http 429' in text
        or any(f'http {code}' in text for code in range(500, 600))
        or 'temporarily' in text
        or 'timeout' in text
        or 'timed out' in text
    )


def resilient_signed_get(self, op, payload=None):
    if op not in _MEDIA_OPS:
        return _ORIGINAL_SIGNED_GET(self, op, payload)

    endpoints = media_endpoints(self)
    if not endpoints:
        return _ORIGINAL_SIGNED_GET(self, op, payload)

    # WordPress image metadata generation can legitimately take longer. If the
    # connection times out after WordPress has created the attachment, trying a
    # second route can race the first request and create a duplicate. Therefore
    # finish gets one direct request with a generous read timeout.
    if op == 'media_finish':
        label, endpoint = endpoints[0]
        params = self._signed_params(op, payload)
        try:
            response = self._stdlib_get(endpoint, params, 180.0)
            return self._decode(response)
        except Exception as exc:
            raise RuntimeError(
                f'Signed GET Bridge ناموفق بود ({op}/{label}): {exc}'
            ) from exc

    errors = []
    # begin/chunk are idempotent. Try the fast direct WordPress route first and
    # only then the homepage / Worker relay. Each attempt receives its own
    # timeout budget instead of sharing the old 15-second global deadline.
    for label, endpoint in endpoints:
        params = self._signed_params(op, payload)
        try:
            response = self._stdlib_get(endpoint, params, 18.0)
            return self._decode(response)
        except bridge.TRANSIENT_ERRORS as exc:
            errors.append(f'{label}: {exc}')
            continue
        except RuntimeError as exc:
            if _retryable_runtime_error(exc):
                errors.append(f'{label}: {exc}')
                continue
            raise
        except (TimeoutError, OSError, httpx.HTTPError) as exc:
            errors.append(f'{label}: {exc}')
            continue
        except Exception as exc:
            errors.append(f'{label}: {exc}')
            continue

    detail = ' | '.join(errors[-3:]) or 'no endpoint responded'
    raise RuntimeError(f'Signed GET Bridge ناموفق بود ({op}): {detail}')


bridge.BridgeWooClient._signed_get = resilient_signed_get
