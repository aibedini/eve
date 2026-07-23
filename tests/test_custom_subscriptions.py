import base64
import binascii
import os
import tempfile
import unittest
from urllib.parse import unquote

_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    Admin, CustomSubscription, CustomSubscriptionConfig, app, db,
)


class CustomSubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()
        try:
            os.unlink(_DB_FILE.name)
        except OSError:
            pass

    def setUp(self):
        CustomSubscriptionConfig.query.delete()
        CustomSubscription.query.delete()
        Admin.query.filter(Admin.username.like('cs-test-%')).delete(synchronize_session=False)
        admin = Admin(username='cs-test-admin', role='superadmin',
                      is_superadmin=True, enabled=True)
        admin.set_password('StrongCustomSubPassword123!')
        db.session.add(admin)
        db.session.commit()
        self.client = app.test_client()
        with self.client.session_transaction() as session_data:
            session_data['admin_id'] = admin.id
            session_data['admin_username'] = admin.username
            session_data['role'] = admin.role
            session_data['is_superadmin'] = True

    def tearDown(self):
        db.session.rollback()
        db.session.remove()

    def _create(self):
        response = self.client.post('/api/custom-subscriptions', json={
            'name': 'Main customers', 'tag_prefix': 'Eve | ',
            'update_interval_min': 30, 'enabled': True,
        })
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()['subscription']

    def test_public_subscription_supports_base64_raw_headers_and_remarks(self):
        subscription = self._create()
        response = self.client.post(
            f"/api/custom-subscriptions/{subscription['id']}/configs",
            json={'configs': 'vless://uuid@example.com:443#Original%20Name\n'
                             'trojan://password@example.net:443'},
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        second = response.get_json()['configs'][1]
        self.client.put(
            f"/api/custom-subscriptions/{subscription['id']}/configs/{second['id']}",
            json={'remark': 'Backup'},
        )

        public = self.client.get(f"/cs/{subscription['token']}")
        self.assertEqual(public.status_code, 200)
        decoded = base64.b64decode(public.get_data(as_text=True)).decode()
        lines = decoded.splitlines()
        self.assertEqual(unquote(lines[0].partition('#')[2]), 'Eve | Original Name')
        self.assertEqual(unquote(lines[1].partition('#')[2]), 'Eve | Backup')
        self.assertEqual(public.headers['Profile-Update-Interval'], '30')
        self.assertEqual(
            base64.b64decode(public.headers['Profile-Title'].split(':', 1)[1]).decode(),
            'Main customers',
        )
        self.assertIn('no-store', public.headers['Cache-Control'])
        raw = self.client.get(f"/cs/{subscription['token']}?format=raw")
        self.assertEqual(raw.get_data(as_text=True), decoded)

    def test_duplicates_validation_disable_and_token_regeneration(self):
        subscription = self._create()
        uri = 'ss://YWVzLTI1Ni1nY206cGFzcw@example.com:443#Primary'
        first = self.client.post(
            f"/api/custom-subscriptions/{subscription['id']}/configs",
            json={'configs': uri},
        )
        self.assertTrue(first.get_json()['success'])
        duplicate = self.client.post(
            f"/api/custom-subscriptions/{subscription['id']}/configs",
            json={'configs': uri},
        )
        self.assertFalse(duplicate.get_json()['success'])
        old_token = subscription['token']
        updated = self.client.put(
            f"/api/custom-subscriptions/{subscription['id']}",
            json={'regenerate_token': True, 'enabled': False},
        ).get_json()['subscription']
        self.assertNotEqual(updated['token'], old_token)
        self.assertEqual(self.client.get(f'/cs/{old_token}').status_code, 404)
        self.assertEqual(self.client.get(f"/cs/{updated['token']}").status_code, 404)

    def test_rejects_unsupported_scheme_and_renders_page(self):
        subscription = self._create()
        rejected = self.client.post(
            f"/api/custom-subscriptions/{subscription['id']}/configs",
            json={'configs': 'https://example.com/config'},
        )
        self.assertFalse(rejected.get_json()['success'])
        page = self.client.get('/custom-subscriptions')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Custom Subscriptions', page.data)

    def test_wireguard_output_keeps_standard_percent_encoding(self):
        subscription = self._create()
        private_key = 'eAa8ZCl94VvnagSvRF4+/lYUyYWFbhDP316H624bk1I='
        public_key = 'zeYgDsVqwHMaSTIqgn76jdDFG/yJVR5ciyLOlNBVYBg='
        encoded_uri = (
            'wireguard://'
            'eAa8ZCl94VvnagSvRF4%2B%2FlYUyYWFbhDP316H624bk1I%3D'
            '@tr3.example:51820'
            '?publickey=zeYgDsVqwHMaSTIqgn76jdDFG%2FyJVR5ciyLOlNBVYBg%3D'
            '&address=10.70.1.2%2F32#TR3'
        )
        created = self.client.post(
            f"/api/custom-subscriptions/{subscription['id']}/configs",
            json={'configs': encoded_uri},
        )
        self.assertEqual(created.status_code, 201, created.get_json())

        raw = self.client.get(
            f"/cs/{subscription['token']}?format=raw",
        ).get_data(as_text=True)
        rendered_base = raw.partition('#')[0]
        # Public output must stay canonical: decoding values into the URI would
        # break standard parsers whenever a Base64 key contains '/' or '+'.
        self.assertEqual(rendered_base, encoded_uri.partition('#')[0])
        self.assertIn('%2F', rendered_base.upper())

        # Eve's own importer (telegram_xray) decodes correctly from the
        # standard link: keys become valid 32-byte Base64 in the Xray JSON.
        from telegram_xray import build_xray_config_from_uri
        config = build_xray_config_from_uri(rendered_base, 12080)
        outbound = next(o for o in config['outbounds'] if o['protocol'] == 'wireguard')
        settings = outbound['settings']
        self.assertEqual(settings['secretKey'], private_key)
        self.assertEqual(settings['peers'][0]['publicKey'], public_key)
        self.assertEqual(settings['peers'][0]['endpoint'], 'tr3.example:51820')
        self.assertEqual(settings['address'], ['10.70.1.2/32'])
        for key in (settings['secretKey'], settings['peers'][0]['publicKey']):
            try:
                decoded = base64.b64decode(key, validate=True)
            except binascii.Error as exc:
                self.fail(f'WireGuard key is not valid Base64: {exc}')
            self.assertEqual(len(decoded), 32)


if __name__ == '__main__':
    unittest.main()
