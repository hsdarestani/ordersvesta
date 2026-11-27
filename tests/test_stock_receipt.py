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

if __name__ == '__main__':
    unittest.main()
