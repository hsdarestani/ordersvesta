"""Read-only Telegram wizard acceptance, no Mahak/Woo side effects."""
import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

os.environ.setdefault('BOTTOKEN','123456:test-token')
os.environ.setdefault('DATA_DIR',tempfile.mkdtemp())

from app import mahak_flow as flow
from app import operations as ops
from app import main as m


class Chat:
    def __init__(self):
        self.messages=[]
    async def send_message(self, message, **kwargs):
        self.messages.append(message)
        return SimpleNamespace(edit_text=AsyncMock())


class Message:
    def __init__(self, value):
        self.text=value


class Update:
    def __init__(self, uid, chat, value=None, action=None):
        self.effective_user=SimpleNamespace(id=uid)
        self.effective_chat=chat
        self.message=Message(value) if value is not None else None
        self.callback_query=SimpleNamespace(
            from_user=self.effective_user,
            data=action,
            message=SimpleNamespace(chat=chat),
            answer=AsyncMock(),
        ) if action is not None else None


class MahakVariableWizardTests(unittest.TestCase):
    def test_two_variants_separate_prices_and_stocks_then_one_preview(self):
        uid=99913310
        chat=Chat()
        data={'name':'تست محصول متغیر محکم','code':'993344',
              'template_code':'92503','template_name':'مداد ابرو',
              'category_id':418342,'unit_name':'عدد',
              'variation_template':{'group_code':'3','sample_values':['قهوه ای']}}
        ops.set_state(uid,'mahak_product','type',data)

        async def go():
            await flow.maybe_callback(Update(uid,chat,action='mahak:type:variable'),None)
            self.assertEqual(ops.get_state(uid)['step'],'attribute_name')
            await flow.maybe_text(Update(uid,chat,value='رنگ'),None)
            await flow.maybe_text(Update(uid,chat,value='صورتی، نود'),None)
            self.assertEqual(ops.get_state(uid)['step'],'var_price_mode')
            await flow.maybe_callback(Update(uid,chat,action='mahak:price:each'),None)
            await flow.maybe_text(Update(uid,chat,value='۱۱۰٬۰۰۰'),None)
            await flow.maybe_text(Update(uid,chat,value='۱۲۰٬۰۰۰'),None)
            self.assertEqual(ops.get_state(uid)['step'],'var_stock_mode')
            await flow.maybe_callback(Update(uid,chat,action='mahak:stock:each'),None)
            await flow.maybe_text(Update(uid,chat,value='۱۰'),None)
            await flow.maybe_text(Update(uid,chat,value='۱۲'),None)
            data=ops.get_state(uid)['data']
            self.assertEqual(ops.get_state(uid)['step'],'photos')
            self.assertEqual(data['price_toman'],'110000')
            self.assertEqual(data['variant_group_code'],'3')
            self.assertEqual(data['variants'],[
                {'option':'صورتی','price_toman':'110000','stock_qty':'10'},
                {'option':'نود','price_toman':'120000','stock_qty':'12'},
            ])
            await flow.maybe_callback(Update(uid,chat,action='mahak:photos_done'),None)
            await flow.maybe_callback(Update(uid,chat,action='mahak:caption_skip'),None)
            await flow.maybe_callback(Update(uid,chat,action='mahak:weight_skip'),None)
            self.assertEqual(ops.get_state(uid)['step'],'preview')
            preview=chat.messages[-1]
            self.assertIn('صورتی',preview)
            self.assertIn('نود',preview)
            self.assertIn('۱۱۰٬۰۰۰',preview)
            self.assertIn('موجودی درخواست‌شده 12',preview)
            self.assertIn('سند انبار',preview)

        with patch.object(m,'access',new=AsyncMock(return_value=True)), \
             patch.object(m,'allowed',return_value=True):
            try:
                asyncio.run(go())
            finally:
                ops.clear_state(uid)

    def test_simple_inventory_request_prepared_but_not_posted(self):
        uid=99913311;chat=Chat()
        data={'name':'کالای ساده','code':'993345','template_code':'8079',
              'template_name':'نمونه','category_id':418353,'unit_name':'عدد'}
        ops.set_state(uid,'mahak_product','type',data)

        async def go():
            await flow.maybe_callback(Update(uid,chat,action='mahak:type:simple'),None)
            await flow.maybe_text(Update(uid,chat,value='۱۹۸٬۰۰۰'),None)
            await flow.maybe_text(Update(uid,chat,value='۲۴'),None)
            state=ops.get_state(uid)
            self.assertEqual(state['step'],'photos')
            self.assertEqual(state['data']['stock_qty'],'24')
            self.assertEqual(state['data']['price_toman'],'198000')
            self.assertNotIn('variants',state['data'])
        with patch.object(m,'access',new=AsyncMock(return_value=True)), \
             patch.object(m,'allowed',return_value=True):
            try:asyncio.run(go())
            finally:ops.clear_state(uid)


if __name__=='__main__':
    unittest.main()
