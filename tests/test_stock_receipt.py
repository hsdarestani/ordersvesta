import unittest
from app.stock_receipt import quantity, price

class StockQuantityTests(unittest.TestCase):
    def test_positive_persian_digits(self):
        self.assertEqual(quantity('۱۲'), 12)
        self.assertEqual(quantity('٣'), 3)
    def test_zero_only_for_absolute_inventory(self):
        self.assertEqual(quantity('۰', allow_zero=True), 0)
    def test_invalid_quantity(self):
        for v in ('0', '-1', '1.5', '10001', '12 units', ''):
            with self.subTest(v=v), self.assertRaises(ValueError):
                quantity(v)

class StockPriceTests(unittest.TestCase):
    def test_positive_grouped_prices(self):
        self.assertEqual(price('۴۵۰٬۰۰۰'), '450000')
        self.assertEqual(price('450,000'), '450000')
        self.assertEqual(price('٣٠٠٠'), '3000')
    def test_invalid_prices(self):
        for v in ('0', '-1', '1.5', '12,34', '10 تومان', '', '1000000000000'):
            with self.subTest(v=v), self.assertRaises(ValueError):
                price(v)

class StockPriceFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_session_refreshes_price_and_displays_real_buttons(self):
        from app import stock_receipt as sr
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        old = {'id': 1, 'identity': 'abc', 'quantity': 2, 'name': 'Test'}
        fresh = dict(old, receivable=True, price_enabled=True, price_version='v2',
                     currency='IRT', regular_price='100000', sale_price='80000',
                     current_price='80000')
        chat = SimpleNamespace(id=9, type='private', send_message=AsyncMock())
        msg = SimpleNamespace(text='3')
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=chat, effective_message=msg)
        state = {'flow': sr.FLOW, 'step': 'quantity', 'data': {'selected': old, 'chat': 9, 'token': 'old'}}
        with patch.object(sr.ops, 'get_state', return_value=state), patch.object(sr.m, 'allowed', return_value=True), patch.object(sr, 'live_state', return_value=state), patch.object(sr, 'session') as save, patch.object(sr, 'call', AsyncMock(return_value={'product': fresh})):
            await sr.text(update, None)
            self.assertEqual(save.call_args.args[1], 'price_choice')
            labels = [button.text for row in chat.send_message.call_args.kwargs['reply_markup'].inline_keyboard for button in row]
            self.assertIn('✏️ تغییر قیمت اصلی', labels)
            self.assertIn('✏️ تغییر قیمت تخفیف', labels)
            self.assertIn('بدون تغییر قیمت', labels)

class NewDiscountTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_existing_discount_still_has_both_price_buttons(self):
        from app import stock_receipt as sr
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        product = dict(id=1, name='Test', quantity=3, regular_price='500000', sale_price='',
                       currency='IRT', price_enabled=True, price_version='v1')
        data = dict(selected=product, token='tok', stock_mode='none')
        chat = SimpleNamespace(send_message=AsyncMock())
        with patch.object(sr, 'session'):
            await sr.price_choices(chat, 1, data)
        rows = chat.send_message.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([b.text for b in rows[0]], ['✏️ تغییر قیمت اصلی', '✏️ تغییر قیمت تخفیف'])

class StockManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_contains_independent_operations(self):
        from app import stock_receipt as sr
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        p = dict(id=1, name='Test', quantity=5, receivable=True, price_enabled=True, price_version='a')
        d = dict(selected=p, token='tok')
        chat = SimpleNamespace(send_message=AsyncMock())
        with patch.object(sr, 'session'):
            await sr.management_choices(chat, 1, d)
        labels = [b.text for row in chat.send_message.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        for label in ('➕ افزایش موجودی','➖ کاهش موجودی','🔢 تعیین موجودی دقیق','✏️ فقط تغییر قیمت'):
            self.assertIn(label, labels)
    def test_preview_modes(self):
        from app.stock_receipt import stock_summary
        for mode, amount, expected in [('increase',2,'7'),('decrease',2,'3'),('set',0,'0')]:
            self.assertIn('موجودی پس از ثبت: '+expected, stock_summary(dict(selected={'quantity':5},stock_mode=mode,amount=amount)))
        self.assertEqual(stock_summary(dict(selected={'quantity':None},stock_mode='none',amount=0)), 'موجودی بدون تغییر')

class GroupPriceTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_keeps_previous_selection(self):
        from app import stock_receipt as sr
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        d=dict(items={'1':{'id':1}},browse_token='same',token='same',parent=1,page=2)
        p=dict(id=2,name='Second',type='variation',quantity=0)
        chat=SimpleNamespace(send_message=AsyncMock(),send_photo=AsyncMock())
        with patch.object(sr,'session'), patch.object(sr,'call',AsyncMock(return_value={})):
            await sr.cards(chat,1,d,{'products':[p]})
        self.assertEqual(d['token'],'same')
        self.assertEqual(set(d['items']),{'1','2'})
    def test_group_validates_every_variant(self):
        from app.stock_receipt import group_price_valid
        group={'items':[{'regular_price':'500','sale_price':'200'},{'regular_price':'300','sale_price':'100'}]}
        group_price_valid(group,'sale','250')
        with self.assertRaises(ValueError): group_price_valid(group,'sale','400')
        with self.assertRaises(ValueError): group_price_valid(group,'regular','150')

if __name__ == '__main__':
    unittest.main()
