import unittest
from app.stock_receipt import quantity

class StockQuantityTests(unittest.TestCase):
    def test_positive_persian_digits(self):
        self.assertEqual(quantity('۱۲'), 12)
        self.assertEqual(quantity('٣'), 3)
    def test_invalid_quantity(self):
        for v in ('0', '-1', '1.5', '10001', '12 units', ''):
            with self.subTest(v=v), self.assertRaises(ValueError):
                quantity(v)

if __name__ == '__main__':
    unittest.main()
