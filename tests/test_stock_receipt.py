import unittest
from app.stock_receipt import quantity, price

class StockQuantityTests(unittest.TestCase):
    def test_positive_persian_digits(self):
        self.assertEqual(quantity('۱۲'), 12)
        self.assertEqual(quantity('٣'), 3)
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

if __name__ == '__main__':
    unittest.main()
