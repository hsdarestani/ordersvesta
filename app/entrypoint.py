import threading

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

# Import runner first: it applies resilient Shopino/Telegram networking and admin login patches.
from app import runner  # noqa: F401
from app import main as m
# Import operations and variable-product flow.
from app import operations as ops
from app import variations as var
# Bridge is imported after product flows so every WooCommerce call uses the local WordPress bridge.
from app import bridge_client  # noqa: F401
# Product UX is imported last: it adds search-first category selection and faster media chunks.
from app import product_ux as pux
# Global runtime optimizations: keep-alive pools, caches, fast SQLite and shared API clients.
from app import performance  # noqa: F401
# Optional-weight wrapper adds a "skip weight" button without changing the rest of the product flow.
from app import weight_optional as wopt
# Site-tracking wrapper adds direct spreadsheet import into the site's tracking table.
from app import site_tracking as st

# Preserve the optional-weight flow under the site-tracking text router.
st.ORIG_TEXT = wopt.text

# Final production routing fixes: strict Shopino/site isolation and instant product wizard start.
from app import routing_hotfix as rh
# Durable queue stores website-tracking files locally until the WordPress site pulls them.
from app import tracking_queue as tq
# Immediate wake worker removes the WP-Cron wait after a tracking file is queued.
from app import tracking_wake as twake
# Parser used by the authenticated pull endpoint.
from app import site_tracking_rows  # noqa: F401
# Tracking status wrapper exposes whether WordPress has actually reached the bot.
from app import tracking_status as tstatus
# Product-route recovery keeps top-level navigation global and repairs stale empty category caches.
from app import product_route_recovery as prr
# Paid Vestaland orders are verified against Hamoon and then written through the signed WP bridge.
from app.vestaland_order_sync import VestalandOrderSyncHTTP

# Wrap the fully composed text chain without bypassing tracking/status behavior.
prr.ORIG_TEXT = tstatus.text

# Apply menu / WooCommerce / tracking routers.
m.start = ops.start
m.text = prr.text
m.callback = wopt.callback
m.photo = pux.photo
m.document = tq.document


def main():
    health = m.ThreadingHTTPServer(('0.0.0.0', 8080), VestalandOrderSyncHTTP)
    threading.Thread(target=health.serve_forever, daemon=True).start()

    request = HTTPXRequest(
        connection_pool_size=32,
        connect_timeout=10.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=5.0,
    )
    updates_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=10.0,
        read_timeout=45.0,
        write_timeout=15.0,
        pool_timeout=5.0,
    )

    a = (
        Application.builder()
        .token(m.TOKEN)
        .request(request)
        .get_updates_request(updates_request)
        .concurrent_updates(8)
        .post_init(twake.startup)
        .build()
    )
    a.add_handler(CommandHandler('start', m.start))
    a.add_handler(CommandHandler('help', m.start))
    a.add_handler(CommandHandler('login', m.login_cmd))
    a.add_handler(CommandHandler('cancel', m.cancel_cmd))
    a.add_handler(CommandHandler('session', m.session))
    a.add_handler(CommandHandler('status', m.status))
    a.add_handler(CommandHandler('allow', m.allow_cmd))
    a.add_handler(CommandHandler('users', m.users))
    a.add_handler(CallbackQueryHandler(m.callback))
    a.add_handler(MessageHandler(filters.PHOTO, m.photo))
    a.add_handler(MessageHandler(filters.Document.ALL, m.document))
    a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, m.text))
    a.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=0.0, timeout=20)


if __name__ == '__main__':
    main()
