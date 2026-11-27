"""Exercise actual production keyboard builders, including late site-tracking override."""
import ast
import unittest
from pathlib import Path
from telegram import ReplyKeyboardMarkup

ROOT = Path(__file__).resolve().parents[1]

def menu(path, name):
    source = (ROOT / path).read_text(encoding='utf-8')
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns = {'ReplyKeyboardMarkup': ReplyKeyboardMarkup}
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), ns)
    return [button.text for row in ns[name]().keyboard for button in row]

class StockMenuTests(unittest.TestCase):
    def test_production_main_override(self):
        self.assertIn('ops.main_menu = main_menu', (ROOT / 'app/site_tracking.py').read_text())
        labels = menu('app/site_tracking.py', 'main_menu')
        self.assertIn('📦 شارژ موجودی', labels)
        self.assertIn('🛍 مدیریت ووکامرس', labels)
        self.assertIn('📦 مدیریت ارسال‌ها', labels)

    def test_default_and_woo_menus(self):
        for name in ('main_menu', 'woo_menu'):
            self.assertIn('📦 شارژ موجودی', menu('app/operations.py', name))

if __name__ == '__main__':
    unittest.main()
