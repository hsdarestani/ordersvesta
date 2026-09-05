import time

from app import bridge_client as bridge

# Keep the fast multi-route retry strategy for reads. Mutating WooCommerce calls
# are different: the origin can take several seconds while WooCommerce persists
# and synchronizes product data. Retrying the same mutation through multiple
# rewrite routes after a 4.5s socket timeout can create duplicate products or
# duplicate variations even though the first PHP request is still finishing.
_READ_SIGNED_GET = bridge.BridgeWooClient._signed_get
_MUTATING_OPS = {'create_product', 'create_variation'}
_MUTATION_TIMEOUT_SECONDS = 75.0


def mutation_safe_signed_get(self, op, payload=None):
    if op not in _MUTATING_OPS:
        return _READ_SIGNED_GET(self, op, payload)

    endpoints = self._bridge_endpoints()
    if not endpoints:
        raise RuntimeError('هیچ مسیر Bridge برای ثبت محصول در دسترس نیست.')

    # One request only. A timeout does not prove the remote mutation failed, so
    # automatically replaying it on another endpoint is unsafe.
    label, endpoint = endpoints[0]
    params = self._signed_params(op, payload)
    started = time.monotonic()
    try:
        response = self._stdlib_get(endpoint, params, _MUTATION_TIMEOUT_SECONDS)
        return self._decode(response)
    except bridge.TRANSIENT_ERRORS as exc:
        elapsed = round(time.monotonic() - started, 1)
        raise RuntimeError(
            f'Bridge mutation timeout ({op}) after {elapsed}s via {label}. '
            'درخواست خودکار تکرار نشد تا محصول/Variation تکراری ساخته نشود.'
        ) from exc
    except RuntimeError:
        raise
    except Exception as exc:
        elapsed = round(time.monotonic() - started, 1)
        raise RuntimeError(
            f'Bridge mutation failed ({op}) after {elapsed}s via {label}: {exc}'
        ) from exc


bridge.BridgeWooClient._signed_get = mutation_safe_signed_get
