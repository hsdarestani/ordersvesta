import os
import tempfile
import unittest

_DATA = tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR', _DATA.name)
os.environ.setdefault('BOTTOKEN', '123456:test-token')

from app import tracking_review_ux as ux


class TrackingReviewUXTests(unittest.TestCase):
    def test_keyboard_shows_visible_order_number_and_no_manual_button(self):
        markup = ux.keyboard('tok123', [{
            'id': 987654,
            'name': 'سارا احمدی',
            'city': 'تهران',
            'display_order_ref': 'V-1204',
            'phone_tail': '4321',
        }])

        buttons = [button for row in markup.inline_keyboard for button in row]
        labels = [button.text for button in buttons]
        callbacks = [button.callback_data for button in buttons]

        self.assertFalse(any(cb.startswith('m:') for cb in callbacks))
        self.assertIn('p:tok123:987654', callbacks)
        self.assertIn('s:tok123', callbacks)
        self.assertTrue(any('سفارش #V-1204' in label for label in labels))

    def test_keyboard_falls_back_to_order_shipping_id(self):
        markup = ux.keyboard('tok789', [{
            'id': 46421,
            'name': 'آرزو یزدانی پوری',
            'city': 'بندرعباس',
            'display_order_ref': '',
            'phone_tail': '',
        }])

        label = markup.inline_keyboard[0][0].text
        self.assertIn('سفارش #46421', label)
        self.assertIn('آرزو یزدانی پوری', label)

    def test_empty_candidates_only_allow_safe_skip(self):
        markup = ux.keyboard('tok456', [])
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].callback_data, 's:tok456')
        self.assertIn('رد این ردیف', buttons[0].text)

    def test_visible_reference_detection(self):
        self.assertEqual(
            ux._display_order_ref({'id': 999, 'order': {'order_number': 'A-77'}}),
            'A-77',
        )
        self.assertEqual(ux._display_order_ref({'id': 999, 'order': {}}), '')


if __name__ == '__main__':
    unittest.main()
