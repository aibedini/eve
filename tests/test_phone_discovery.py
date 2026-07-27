import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch


_DB_FILE = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB_FILE.close()
os.environ['DATABASE_URL'] = f"sqlite:///{_DB_FILE.name.replace(os.sep, '/')}"
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

from app import (  # noqa: E402
    GLOBAL_SERVER_DATA,
    CustomerAccount,
    OwnershipClaim,
    OwnershipClaimItem,
    Server,
    ServiceOwnership,
    TelegramIdentity,
    app,
    db,
    discover_phone_ownership_claim,
)


class PhoneDiscoveryTests(unittest.TestCase):
    """Bot first-contact discovery: verified phone must match client email OR
    comment, in any common Iranian mobile format."""

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
        OwnershipClaimItem.query.delete()
        OwnershipClaim.query.delete()
        TelegramIdentity.query.delete()
        ServiceOwnership.query.delete()
        CustomerAccount.query.delete()
        Server.query.delete()
        db.session.commit()
        self._orig_inbounds = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = []

    def tearDown(self):
        GLOBAL_SERVER_DATA['inbounds'] = self._orig_inbounds
        db.session.rollback()
        db.session.remove()

    def _identity(self, phone='989125551234', tg_id=71001):
        customer = CustomerAccount(
            primary_phone=phone, phone_verified_at=datetime.utcnow())
        identity = TelegramIdentity(
            customer=customer, telegram_user_id=tg_id, telegram_chat_id=tg_id,
            phone_normalized=phone, phone_verified_at=datetime.utcnow(),
        )
        server = Server(
            name=f'Discovery {tg_id}', host=f'https://discovery-{tg_id}.test',
            username='u', password='p',
        )
        db.session.add_all([customer, identity, server])
        db.session.commit()
        return identity, server

    def _discover(self, identity, inbounds):
        previous = GLOBAL_SERVER_DATA.get('inbounds')
        GLOBAL_SERVER_DATA['inbounds'] = inbounds
        try:
            with patch('panel.services.ownership.load_snapshot_from_redis',
                       return_value=False):
                claim = discover_phone_ownership_claim(identity)
                db.session.commit()
        finally:
            GLOBAL_SERVER_DATA['inbounds'] = previous
        return claim

    def test_phone_in_comment_is_discovered(self):
        identity, server = self._identity(phone='989125550099', tg_id=71002)
        claim = self._discover(identity, [{
            'server_id': server.id, 'id': 1,
            'clients': [{
                'id': 'uuid-comment-1', 'subId': 'sub-token-1',
                'email': 'g300-no-number-here',
                'comment': '0912 555 0099',
            }],
        }])
        self.assertIsNotNone(claim)
        self.assertEqual(len(claim.items), 1)
        self.assertEqual(claim.items[0].client_uuid, 'uuid-comment-1')
        self.assertEqual(claim.items[0].match_reason, 'verified_phone_in_client_comment')
        self.assertEqual(claim.items[0].match_score, 95)

    def test_phone_in_raw_client_comment_is_discovered(self):
        identity, server = self._identity(phone='989125550098', tg_id=71003)
        claim = self._discover(identity, [{
            'server_id': server.id, 'id': 1,
            'clients': [{
                'id': 'uuid-raw-comment-1', 'subId': 'sub-token-2',
                'email': 'g301-still-no-number',
                'raw_client': {'comment': '+989125550098'},
            }],
        }])
        self.assertIsNotNone(claim)
        self.assertEqual(len(claim.items), 1)
        self.assertEqual(claim.items[0].match_reason, 'verified_phone_in_client_comment')

    def test_email_still_matches_with_score_100(self):
        identity, server = self._identity(phone='989125550097', tg_id=71004)
        claim = self._discover(identity, [{
            'server_id': server.id, 'id': 1,
            'clients': [{
                'id': 'uuid-email-1', 'subId': 'sub-token-3',
                'email': 'g302-09125550097',
            }],
        }])
        self.assertIsNotNone(claim)
        self.assertEqual(claim.items[0].match_reason, 'verified_phone_in_client_name')
        self.assertEqual(claim.items[0].match_score, 100)

    def test_format_variants_all_match(self):
        variants = [
            '0912 555 0096',
            '+98 912 555 0096',
            '00989125550096',
            '۰۹۱۲۵۵۵۰۰۹۶',  # Persian digits
            '912-555-0096',
        ]
        identity, server = self._identity(phone='989125550096', tg_id=71010)
        for i, stored in enumerate(variants):
            with self.subTest(stored=stored):
                # Fresh claim per variant (a pending claim is otherwise reused).
                OwnershipClaimItem.query.delete()
                OwnershipClaim.query.delete()
                db.session.commit()
                claim = self._discover(identity, [{
                    'server_id': server.id, 'id': 1,
                    'clients': [{
                        'id': f'uuid-fmt-{i}', 'subId': f'sub-fmt-{i}',
                        'email': 'no-number',
                        'comment': stored,
                    }],
                }])
                self.assertIsNotNone(claim)
                self.assertTrue(any(
                    item.client_uuid == f'uuid-fmt-{i}' for item in claim.items))

    def test_unrelated_phone_is_not_matched(self):
        identity, server = self._identity(phone='989125550095', tg_id=71020)
        claim = self._discover(identity, [{
            'server_id': server.id, 'id': 1,
            'clients': [{
                'id': 'uuid-other-1', 'subId': 'sub-other-1',
                'email': 'g303-09120000000',
                'comment': 'some note without the number',
            }],
        }])
        self.assertIsNone(claim)


if __name__ == '__main__':
    unittest.main()
