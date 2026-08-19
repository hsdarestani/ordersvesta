import threading

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

# Import runner first: it applies resilient Shopino/Telegram networking and admin login patches.
from app import runner  # noqa: F401
from app import main as m
# Import operations after runner so its fallback handlers point to the patched admin flows.
from app import operations as ops


# Apply menu / WooCommerce routers.
m.start = ops.start
m.text = ops.text
m.callback = ops.callback
m.photo = ops.photo


def main():
    health = m.ThreadingHTTPServer(('0.0.0.0', 8080), m.Health)
    threading.Thread(target=health.serve_forever, daemon=True).start()

    a = Application.builder().token(m.TOKEN).build()
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
    a.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()
