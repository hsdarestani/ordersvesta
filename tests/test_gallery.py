import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

_DATA = tempfile.TemporaryDirectory()
os.environ.setdefault('DATA_DIR', _DATA.name)
os.environ.setdefault('BOTTOKEN', '123456:test-token')

from app import product_ux as pf


class GalleryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_album_retains_successes_and_identifies_failed_positions(self):
        pf.ops.set_state(77, 'woo_product', 'gallery', {'cover': {'id': 10}, 'gallery': []})
        pf.GALLERY_BATCHES[77] = {'group_id': 'album', 'file_ids': [str(i) for i in range(7)]}
        bot = AsyncMock()
        async def upload(bot, fid, index):
            if index in {1, 3, 5, 6}:
                raise RuntimeError('Bridge HTTP 503')
            return {'id': 100 + index, 'src': ''}
        with patch.object(pf, '_upload_telegram_photo', upload), \
             patch.object(pf, 'GALLERY_DEBOUNCE_SECONDS', 0):
            await pf._process_gallery_batch(77, 77, 'album', bot)
        state = pf.ops.get_state(77)
        data = state['data']
        self.assertEqual(state['step'], 'gallery')
        self.assertEqual([x['id'] for x in data['gallery']], [100, 102, 104])
        self.assertEqual(data['images'][0]['id'], 10)
        final_text = bot.send_message.return_value.edit_text.call_args.args[0]
        self.assertIn('3/7', final_text)
        self.assertIn('2، 4، 6، 7', final_text)
