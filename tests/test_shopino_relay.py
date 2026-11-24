"""Pure, no-network safety regressions for the Iranian Shopino egress relay."""
import ast
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import httpx
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELAY = (ROOT / 'app' / 'shopino_relay.py').read_text(encoding='utf-8')
ENTRY = (ROOT / 'app' / 'entrypoint.py').read_text(encoding='utf-8')


class ShopinoRelayWiringTests(unittest.TestCase):
    def test_encrypted_request_and_fixed_shopino_host(self):
        ast.parse(RELAY)
        self.assertIn('SealedBox(_get_public_key()).encrypt(raw)', RELAY)
        self.assertIn("parsed.hostname != 'api.shopino.app'", RELAY)
        self.assertIn('Shopino request path is not allowlisted.', RELAY)
        self.assertIn("if op != 'shopino_call':", RELAY)
        self.assertIn("m.Shopino = RelayShopino", RELAY)

    def test_telegram_still_uses_shopino_excel_route(self):
        self.assertIn('from app import shopino_relay', ENTRY)
        self.assertIn('return await st.ORIG_DOCUMENT(update, ctx)', ENTRY)
        self.assertIn("if state and state.get('flow') == 'shopino_tracking':", ENTRY)
        self.assertNotIn('mahak.maybe_', ENTRY)

    def test_accounting_is_not_on_tracking_path(self):
        self.assertNotIn('mahak_submit', RELAY)
        self.assertNotIn('create_product', RELAY)



    def test_order_patch_and_http_transport_are_separate(self):
        # Exercise the real class bodies without installing the application's
        # global monkey patches or opening a production session.
        main = ast.parse((ROOT / 'app' / 'main.py').read_text(encoding='utf-8'))
        base_class = next(n for n in main.body if isinstance(n, ast.ClassDef) and n.name == 'Shopino')
        base = 'https://api.shopino.app/api/v1/shop-panel'
        scope = {'BASE': base, 'SHOP': '7944'}
        exec(compile(ast.Module(body=[base_class], type_ignores=[]), '<Shopino>', 'exec'), scope)
        relay_ast = ast.parse(RELAY)
        classes = [n for n in relay_ast.body if isinstance(n, ast.ClassDef)]
        call = Mock(return_value={'status_code': 200, 'body': {'ok': True}})
        auth_error = type('AuthError', (RuntimeError,), {})
        ns = {
            'm': SimpleNamespace(Shopino=scope['Shopino'], SHOP='7944', AuthError=auth_error),
            'httpx': httpx, 're': re, 'urlsplit': urlsplit, 'parse_qs': parse_qs,
            '_call_shopino': call,
        }
        scope['AuthError'] = auth_error
        exec(compile(ast.Module(body=classes, type_ignores=[]), '<RelayShopino>', 'exec'), ns)
        client = ns['RelayShopino']('test-session')
        url = f'{base}/shops/7944/order-shippings/123/'
        self.assertIsNot(client.c, client)
        self.assertTrue(client.patch(123, '001234', 'post'))
        call.assert_called_once_with({
            'action': 'orders_patch', 'session': 'test-session', 'id': 123,
            'tracking_code': '001234', 'type': 'post',
        })
        call.reset_mock()
        response = client.c.patch(url, json={'tracking_code': '0099', 'type': 'post'})
        self.assertEqual(response.status_code, 200)
        call.assert_called_once_with({
            'action': 'orders_patch', 'session': 'test-session', 'id': 123,
            'tracking_code': '0099', 'type': 'post',
        })
        call.reset_mock()
        self.assertTrue(client.patch(123, '0001'))
        self.assertEqual(call.call_args.args[0]['type'], 'post')
        call.reset_mock()
        call.return_value = {'status_code': 401, 'body': {}}
        with self.assertRaises(auth_error):
            client.patch(123, '0001', 'post')
        call.assert_called_once()
        call.reset_mock()
        call.return_value = {'status_code': 200, 'body': {'count': 1}}
        self.assertEqual(client.probe(), 1)
        call.assert_called_once_with({
            'action': 'orders_list', 'session': 'test-session', 'page': 1, 'page_size': 1,
        })

if __name__ == '__main__':
    unittest.main()
