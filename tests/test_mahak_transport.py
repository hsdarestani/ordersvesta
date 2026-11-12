import os
import tempfile
import unittest

os.environ.setdefault('BOTTOKEN', '123456:test-token')
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp())

from app import mahak_transport


class FakeBridge:
    url = 'https://vesta.example.test'

    def __init__(self, *, fail_first=False, always_fail=False):
        self.calls = []
        self.fail_first = fail_first
        self.always_fail = always_fail

    def _signed_params(self, operation, payload=None):
        return {'op': operation, 'payload': payload or {}}

    def _stdlib_get(self, endpoint, params, timeout):
        self.calls.append((endpoint, params, timeout))
        if self.always_fail or (self.fail_first and len(self.calls) == 1):
            raise TimeoutError('read operation timed out')
        return {'status': 'ok'}

    def _decode(self, response):
        return response


class MahakTransportTests(unittest.TestCase):
    def test_read_timeout_is_longer_than_old_four_point_five_seconds(self):
        client = FakeBridge()
        result = mahak_transport.mahak_safe_signed_get(
            client, 'mahak_template', {'template_code':'8079'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(client.calls), 1)
        self.assertGreaterEqual(client.calls[0][2], 20)

    def test_read_retries_with_fresh_route(self):
        client = FakeBridge(fail_first=True)
        result = mahak_transport.mahak_safe_signed_get(
            client, 'mahak_status', {'request_key':'f'*32})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(len(client.calls), 2)

    def test_write_never_retries_on_timeout(self):
        client = FakeBridge(always_fail=True)
        with self.assertRaises(RuntimeError):
            mahak_transport.mahak_safe_signed_get(
                client, 'mahak_submit', {'request_key':'a'*32})
        self.assertEqual(len(client.calls), 1)
        self.assertGreaterEqual(client.calls[0][2], 60)


if __name__ == '__main__':
    unittest.main()
