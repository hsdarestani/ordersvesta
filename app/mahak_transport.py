"""Long-running Mahak API transport policy, isolated from fast WooCommerce operations.

The general Vesta Bridge transport budgets 4.5 seconds per route; Mahak catalog
reads sometimes need longer. SaveAllData writes get a *single* attempt: a
timeout is never proof that the product was not created.
"""
import os
import time

from app import bridge_client as bridge

_PREVIOUS = bridge.BridgeWooClient._signed_get
_READ_OPS = {'mahak_template', 'mahak_status', 'mahak_site_search', 'mahak_site_status'}
_WRITE_OPS = {'mahak_submit', 'mahak_site_import'}


def mahak_endpoints(client):
    # Direct site is faster than the Cloudflare Worker for product catalog reads.
    url = str(client.url).rstrip('/')
    result = [('site', url + '/')]
    relay = str(os.getenv('BRIDGE_RELAY_URL') or '').rstrip('/')
    if relay and relay != url:
        result.append(('relay', relay + '/'))
    result.append(('admin-ajax', url + '/wp-admin/admin-ajax.php'))
    return tuple(result)


def _retryable(exc):
    s = str(exc).lower()
    return (
        'timeout' in s or 'timed out' in s or 'temporar' in s
        or any('http ' + str(code) in s for code in (408, 429, 502, 503, 504))
        or any('http ' + str(code) in s for code in range(500, 600))
        or 'دریافت فهرست کالاهای محک' in str(exc)
    )


def mahak_safe_signed_get(self, op, payload=None):
    if op not in _READ_OPS and op not in _WRITE_OPS:
        return _PREVIOUS(self, op, payload)

    endpoints = mahak_endpoints(self)
    if op in _WRITE_OPS:
        # Crucial: do not replay a timed-out write to a different route.
        name, endpoint = endpoints[0]
        params = self._signed_params(op, payload)
        try:
            return self._decode(self._stdlib_get(endpoint, params, 75.0))
        except Exception as exc:
            raise RuntimeError(
                'نتیجه ثبت در API محک قطعی نیست (مسیر ' + name + '): '
                + str(exc)[:200]
                + '؛ ارسال خودکار تکرار نشد. فقط وضعیت همان درخواست را بررسی کنید.'
            ) from exc

    errors = []
    for name, endpoint in endpoints:
        params = self._signed_params(op, payload)
        try:
            # Real Bazara GET can take >4.5 seconds even when healthy.
            return self._decode(self._stdlib_get(endpoint, params, 25.0))
        except Exception as exc:
            if not _retryable(exc):
                raise
            errors.append(name + ': ' + str(exc)[:120])
            # Only catalog reads/status checks are retried.
            time.sleep(0.2)
    raise RuntimeError('خواندن اطلاعات محک بعد از تلاش در مسیرهای مجاز ناموفق بود: '
                       + ' | '.join(errors[-3:]))


bridge.BridgeWooClient._signed_get = mahak_safe_signed_get
