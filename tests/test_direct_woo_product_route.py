"""Regression tests for switching product creation back to direct WooCommerce.

Tests isolate the async routers without importing the live Telegram bot,
so they never connect to Telegram, WooCommerce, or an accounting backend.
"""
import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


SOURCE = (Path(__file__).resolve().parents[1] / "app" / "entrypoint.py").read_text(encoding="utf-8")


def make_router(name, namespace):
    tree = ast.parse(SOURCE)
    func = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    isolated = ast.fix_missing_locations(ast.Module(body=[func], type_ignores=[]))
    exec(compile(isolated, "<entrypoint-router>", "exec"), namespace)
    return namespace[name]


class LegacyProductRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ops = SimpleNamespace(
            begin_product=AsyncMock(return_value="started-woo"),
            reset_user_flow=Mock(),
            main_menu=Mock(return_value="main-menu"),
        )
        self.sms = SimpleNamespace(
            clear_state=Mock(),
            text=AsyncMock(return_value="sms-fallback"),
            callback=AsyncMock(return_value="callback-fallback"),
        )
        self.m = SimpleNamespace(access=AsyncMock(return_value=True), allowed=Mock(return_value=True))
        self.ns = {
            "m": self.m,
            "ops": self.ops,
            "sms_flow": self.sms,
            "_base_text_handler": object(),
            "_base_callback_handler": object(),
            "LEGACY_ACCOUNTING_LABELS": {
                "🧾 ثبت کالا در محک",
                "🧾 ساخت کالای جدید در محک",
                "📸 عکس و محتوای کالای محک",
                "📥 کالای محک به سایت",
            },
        }
        self.text_router = make_router("routed_text", self.ns)
        self.callback_router = make_router("routed_callback", self.ns)

    def message(self, value):
        return SimpleNamespace(
            message=SimpleNamespace(text=value, reply_text=AsyncMock(return_value="menu")),
            effective_user=SimpleNamespace(id=42),
        )

    async def test_product_button_bypasses_accounting_and_stale_sms_state(self):
        update = self.message("➕ ثبت محصول جدید")
        result = await self.text_router(update, None)
        self.assertEqual(result, "started-woo")
        self.ops.begin_product.assert_awaited_once_with(update)
        self.sms.clear_state.assert_called_once_with(42)
        self.sms.text.assert_not_awaited()

    async def test_old_accounting_buttons_cannot_submit(self):
        for value in self.ns["LEGACY_ACCOUNTING_LABELS"]:
            update = self.message(value)
            await self.text_router(update, None)
            self.ops.begin_product.assert_not_awaited()
            update.message.reply_text.assert_awaited_once()
        self.sms.text.assert_not_awaited()

    async def test_old_accounting_inline_callbacks_cannot_submit(self):
        for value in ("mahak:submit", "mahaksite:import", "mahakcontent:save"):
            q = SimpleNamespace(data=value, from_user=SimpleNamespace(id=42), answer=AsyncMock())
            await self.callback_router(SimpleNamespace(callback_query=q), None)
            q.answer.assert_awaited_once()
        self.sms.callback.assert_not_awaited()

    async def test_other_messages_still_reach_existing_flow(self):
        update = self.message("محصول تستی")
        self.assertEqual(await self.text_router(update, None), "sms-fallback")
        self.sms.text.assert_awaited_once_with(update, None, self.ns["_base_text_handler"])

    async def test_product_creation_requires_authorized_user(self):
        self.m.access.return_value = False
        await self.text_router(self.message("➕ ثبت محصول جدید"), None)
        self.ops.begin_product.assert_not_awaited()


class ProductionWiringTests(unittest.TestCase):
    def test_no_accounting_product_wizard_loaded(self):
        self.assertNotIn("from app import mahak_flow", SOURCE)
        self.assertNotIn("from app import mahak_transport", SOURCE)
        self.assertNotIn("CommandHandler('mahak'", SOURCE)
        self.assertNotIn("mahak.maybe_", SOURCE)
        self.assertIn("m.photo = pux.photo", SOURCE)

    def test_original_woo_bridge_product_path_is_intact(self):
        root = Path(__file__).resolve().parents[1]
        ops = (root / "app" / "operations.py").read_text(encoding="utf-8")
        bridge = (root / "wordpress" / "vesta-bot-bridge" / "vesta-bot-bridge.php").read_text(encoding="utf-8")
        self.assertIn("return await begin_product(update)", ops)
        self.assertIn("set_state(uid, 'woo_product', 'name'", ops)
        self.assertIn("function vbb_create_product($data)", bridge)


if __name__ == "__main__":
    unittest.main()
