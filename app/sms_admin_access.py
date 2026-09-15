import asyncio

from app import main as m
from app import weight_optional as wopt


async def callback(update, ctx):
    """Allow every authorized bot admin to use tracking-SMS actions.

    Only the SMS test-send and final send confirmation are widened from owner-only
    to the existing authorized-admin list. All other callback routing and owner-only
    security boundaries (Shopino login/session, granting access, etc.) stay unchanged.
    """
    q = update.callback_query
    cb = q.data or ''
    uid = q.from_user.id

    if cb.startswith('test:'):
        if not m.allowed(uid):
            return await q.answer('دسترسی ندارید', show_alert=True)
        await q.answer()
        phone = m.get(f'sms_test_phone_{uid}')
        code = m.get(f'sms_test_code_{uid}')
        if not phone or not code:
            return await q.edit_message_text('❌ اطلاعات تست کامل نیست؛ تست پیامک را دوباره شروع کنید.')
        try:
            await asyncio.to_thread(m.melipayamak_send, phone, m.SMS_TEXT.format(code=code))
            m.setv(f'sms_test_{uid}', '')
            return await q.edit_message_text('✅ پیامک تست با موفقیت ارسال شد.')
        except Exception as exc:
            return await q.edit_message_text(f'❌ ارسال تست ناموفق بود: {exc}')

    if cb.startswith('z:'):
        if not m.allowed(uid):
            return await q.answer('دسترسی ندارید', show_alert=True)
        await q.answer()
        token = cb.split(':', 1)[1]
        p = m.pending(token)
        if not p or p['status'] != 'pending':
            return await q.edit_message_text('این مورد قبلاً بررسی شده.')
        payload = p['payload']
        if not payload.get('items'):
            return await q.edit_message_text('❌ موردی برای ارسال وجود ندارد.')
        try:
            sent = 0
            for item in payload['items']:
                text = m.SMS_TEXT.format(code=item['code'])
                await asyncio.to_thread(m.melipayamak_send, item['phone'], text)
                sent += 1
            m.done(token, 'sms_sent')
            return await q.edit_message_text(f'✅ {sent} پیامک ارسال شد.')
        except Exception as exc:
            return await q.edit_message_text(f'❌ خطا: {exc}')

    return await wopt.callback(update, ctx)
