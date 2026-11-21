"""Pure, no-network safety regressions for the Iranian Shopino egress relay."""
import ast
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


if __name__ == '__main__':
    unittest.main()
