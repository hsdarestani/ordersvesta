import sqlite3
import threading
import time

import httpx

from app import bridge_client as bridge
from app import main as m
from app import operations as ops


# ---------------------------------------------------------------------------
# SQLite: keep one connection per thread instead of opening a new file handle
# for every button press / state read. WAL + NORMAL sync is appropriate for the
# bot's small local state DB and substantially cuts UI-state latency.
# ---------------------------------------------------------------------------
_db_local = threading.local()


def fast_db():
    conn = getattr(_db_local, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(m.DB, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.execute('PRAGMA busy_timeout=5000')
        _db_local.conn = conn
    return conn


m.db = fast_db


# ---------------------------------------------------------------------------
# Tiny in-memory cache for values read on almost every Telegram update.
# Writes invalidate immediately, so login/access changes are still instant.
# ---------------------------------------------------------------------------
_orig_get = m.get
_orig_setv = m.setv
_kv_lock = threading.Lock()
_kv_cache = {}
KV_TTL = 30.0


def cached_get(key):
    if key not in {'owner', 'session'}:
        return _orig_get(key)
    now = time.monotonic()
    with _kv_lock:
        item = _kv_cache.get(key)
        if item and now - item[0] < KV_TTL:
            return item[1]
    value = _orig_get(key)
    with _kv_lock:
        _kv_cache[key] = (now, value)
    return value


def cached_setv(key, value):
    result = _orig_setv(key, value)
    with _kv_lock:
        _kv_cache.pop(key, None)
    return result


m.get = cached_get
m.setv = cached_setv


# Cache the allowed-user set briefly. This removes multiple SQLite reads from
# every menu/callback event while /allow remains effective immediately enough.
_orig_allowed = m.allowed
_access_lock = threading.Lock()
_access_cache = {}
ACCESS_TTL = 15.0


def cached_allowed(uid):
    uid = int(uid)
    now = time.monotonic()
    with _access_lock:
        item = _access_cache.get(uid)
        if item and now - item[0] < ACCESS_TTL:
            return item[1]
    value = bool(_orig_allowed(uid))
    with _access_lock:
        _access_cache[uid] = (now, value)
    return value


m.allowed = cached_allowed


# ---------------------------------------------------------------------------
# Shopino: reuse one HTTPX client/session instead of doing a fresh TCP/TLS
# handshake for status checks and operations every time a button is pressed.
# ---------------------------------------------------------------------------
_shopino_lock = threading.Lock()
_shopino_sid = None
_shopino_client = None


def cached_shopino_api():
    global _shopino_sid, _shopino_client
    sid = m.get('session')
    if not sid:
        raise m.AuthError('هنوز وارد شاپینو نشده‌اید. /login را بزنید.')
    with _shopino_lock:
        if _shopino_client is None or _shopino_sid != sid:
            try:
                if _shopino_client is not None:
                    _shopino_client.c.close()
            except Exception:
                pass
            _shopino_client = m.Shopino(sid)
            _shopino_sid = sid
        return _shopino_client


m.api = cached_shopino_api


# ---------------------------------------------------------------------------
# Woo/Bridge: reuse keep-alive connections and cache expensive read-only calls.
# This is especially noticeable on "new product", category search and status.
# ---------------------------------------------------------------------------
_bridge_lock = threading.Lock()
_bridge_clients = {}
_cache_lock = threading.Lock()
_probe_cache = {}
_category_cache = {}

_orig_bridge_init = bridge.BridgeWooClient.__init__
_orig_probe = bridge.BridgeWooClient.probe
_orig_categories = bridge.BridgeWooClient.categories


def pooled_bridge_init(self):
    self.url = (ops.cfg_get('url') or '').rstrip('/')
    self.token = ops.cfg_get('bridge_token') or ''
    if not self.url or not self.token:
        raise RuntimeError('Bridge ووکامرس تنظیم نشده است. از «🔌 اتصال ووکامرس» استفاده کنید.')
    key = (self.url, self.token)
    with _bridge_lock:
        client = _bridge_clients.get(key)
        if client is None:
            client = httpx.Client(
                timeout=httpx.Timeout(180.0, connect=15.0, pool=5.0),
                follow_redirects=True,
                headers=bridge.BROWSER_HEADERS,
                limits=httpx.Limits(max_connections=40, max_keepalive_connections=20, keepalive_expiry=60.0),
            )
            _bridge_clients[key] = client
        self.c = client


def cached_probe(self):
    key = (self.url, self.token)
    now = time.monotonic()
    with _cache_lock:
        item = _probe_cache.get(key)
        if item and now - item[0] < 30.0:
            return item[1]
    value = _orig_probe(self)
    with _cache_lock:
        _probe_cache[key] = (now, value)
    return value


def cached_categories(self):
    key = (self.url, self.token)
    now = time.monotonic()
    with _cache_lock:
        item = _category_cache.get(key)
        if item and now - item[0] < 600.0:
            return list(item[1])
    value = _orig_categories(self)
    with _cache_lock:
        _category_cache[key] = (now, list(value))
    return value


bridge.BridgeWooClient.__init__ = pooled_bridge_init
bridge.BridgeWooClient.probe = cached_probe
bridge.BridgeWooClient.categories = cached_categories


# ops.WooClient already points at BridgeWooClient after bridge_client import.
ops.WooClient = bridge.BridgeWooClient
