import json
import time
from urllib.parse import urlparse

from app import main as m
from app.tracking_pull import TrackingPullHTTP


def record_site_hit(path, remote=''):
    payload = {
        'time': time.time(),
        'path': str(path or ''),
        'remote': str(remote or ''),
    }
    try:
        m.setv('site_tracking_last_hit', json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


class TrackingPullTelemetryHTTP(TrackingPullHTTP):
    """Tracking pull HTTP server with minimal reachability telemetry.

    The timestamp is written before authentication. Therefore the Telegram status
    screen can distinguish "WordPress never reached the bot" from token/signature
    problems without exposing any secret values.
    """

    def do_GET(self):
        path = urlparse(self.path).path
        if path in {'/tracking-pull', '/tracking-ack'}:
            remote = ''
            try:
                remote = self.client_address[0]
            except Exception:
                pass
            record_site_hit(path, remote)
        return super().do_GET()
