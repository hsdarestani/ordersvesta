# OrdersVesta Shopino Tracking Bot

Telegram bot for bulk-importing postal tracking codes from Vesta Excel files into Shopino.

## Flow

1. Admin sets a Shopino browser `sessionid` with `/session`.
2. User sends an `.xlsx` or `.xlsm` postal Excel file to the bot.
3. The bot reads barcode / destination / recipient columns (with C/E/H/I fallbacks for the Vesta export).
4. It normalizes Persian/Arabic characters, whitespace and ZWNJ, then fuzzy-matches the recipient against Shopino orders.
5. Clear matches are patched automatically. Ambiguous matches are shown as Telegram buttons for human confirmation.
6. Existing different tracking codes are never overwritten automatically.

## Shopino authentication

The discovered Shopino panel API uses the authenticated browser cookie `sessionid`. The bot stores only that value in its persistent SQLite volume. If Shopino expires the session, `/status` reports it and the owner can refresh it with `/session <value>`.

## Security

The first Telegram account to run `/start` becomes the owner. Only the owner may change the Shopino session or grant access with `/allow <telegram_id>`.

## Health

The container exposes `http://127.0.0.1:8094/health` on the server. The deployment workflow configures `ordersvesta.smarbiz.sbs` through nginx when possible.
