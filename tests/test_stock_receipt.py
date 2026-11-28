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

class VariableMenuTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        return dict(id=77,name='Ice cream',count=6,currency='IRT',version='v1',
            items=[dict(id=100+i,name='Ice cream',type='variation',attributes='رنگ '+str(i),
                        quantity=i,regular_price='500000',sale_price='',parent_id=77,
                        price_enabled=True,price_version='p',receivable=True,identity='id'+str(i)) for i in range(6)])

    async def test_parent_selection_immediately_shows_group_and_stock(self):
        from app import stock_receipt as sr
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        g=self.fixture()
        state=dict(flow=sr.FLOW,step='choose',data=dict(token='t',chat=9,items={'77':{'id':77}},parent=0))
        chat=SimpleNamespace(id=9,type='private',send_message=AsyncMock())
        q=SimpleNamespace(data='stock:pick:t:77',from_user=SimpleNamespace(id=1),answer=AsyncMock())
        update=SimpleNamespace(callback_query=q,effective_chat=chat)
        with patch.object(sr.m,'allowed',return_value=True),patch.object(sr,'live_state',return_value=state),patch.object(sr,'session') as save,patch.object(sr,'call',AsyncMock(side_effect=[{'product':{'id':77,'type':'variable'}},g])):
            await sr.callback(update,None)
        self.assertEqual(save.call_args.args[1],'group_choice')
        rows=chat.send_message.call_args.kwargs['reply_markup'].inline_keyboard
        labels=[b.text for row in rows for b in row]
        self.assertIn('🏷️ قیمت اصلی همه مدل‌ها',labels)
        self.assertIn('🏷️ قیمت تخفیفی همه مدل‌ها',labels)
        self.assertIn('📦 مدیریت موجودی / انتخاب مدل',labels)

    async def test_stock_list_contains_all_six_and_selects_last_model(self):
        from app import stock_receipt as sr
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        g=self.fixture()
        chat=SimpleNamespace(id=9,type='private',send_message=AsyncMock(),send_photo=AsyncMock())
        d=dict(chat=9,parent=77)
        with patch.object(sr,'session') as save,patch.object(sr,'call',AsyncMock(return_value=g)):
            await sr.show_models(chat,1,d,77)
        self.assertEqual(len(d['items']),6)
        self.assertEqual(save.call_args.args[1],'choose')
        rows=chat.send_message.call_args.kwargs['reply_markup'].inline_keyboard
        last=next(b.callback_data for row in rows for b in row if b.callback_data.endswith(':105'))
        self.assertEqual(chat.send_photo.await_count,0)
        state=dict(flow=sr.FLOW,step='choose',data=d)
        q=SimpleNamespace(data=last,from_user=SimpleNamespace(id=1),answer=AsyncMock())
        with patch.object(sr.m,'allowed',return_value=True),patch.object(sr,'live_state',return_value=state),patch.object(sr,'session') as saved,patch.object(sr,'call',AsyncMock(return_value={'product':g['items'][-1]})):
            await sr.callback(SimpleNamespace(callback_query=q,effective_chat=chat),None)
        self.assertEqual(saved.call_args.args[1],'management')
        labels=[b.text for row in chat.send_message.call_args.kwargs['reply_markup'].inline_keyboard for b in row]
        self.assertIn('➕ افزایش موجودی',labels)
        self.assertIn('➖ کاهش موجودی',labels)
        self.assertIn('🔢 تعیین موجودی دقیق',labels)
        self.assertEqual(d['selected']['id'],105)

if __name__ == '__main__':
    unittest.main()
