"""Online chat scripts: activating AND switching back off.

Exactly one snippet (or none) is injected into the subscription page, and the panel
only offered "Activate". Once a script was active its card showed a disabled "Active"
button - there was no way back short of deleting the snippet and pasting it in again.
The API had no counterpart to /activate either, so the UI was not the only gap.

These tests pin both halves: the endpoint clears the flag without touching any other
row, and the page offers the control (card and edit modal) that calls it.
"""
import base64
import os
import tempfile
import unittest
from pathlib import Path

_DB = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_DB.close()
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB.name.replace(os.sep, '/'))
os.environ.setdefault('EVE_SKIP_IMPORT_MIGRATIONS', '1')
os.environ.setdefault('SESSION_SECRET', 'eve-test-session-secret')
os.environ.setdefault('SERVER_PASSWORD_KEY',
                      base64.urlsafe_b64encode(b'eve-test-key-32-bytes-padded-000').decode())
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'

import app as app_module  # noqa: E402
from panel.extensions import db  # noqa: E402
from panel.models import Admin, OnlineChatScript  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / 'templates' / 'sub_manager.html'


class OnlineChatScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app_module.app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        OnlineChatScript.query.delete()
        Admin.query.filter_by(username='chat-admin').delete()
        db.session.commit()
        admin = Admin(username='chat-admin', password_hash='x', role='superadmin',
                      is_superadmin=True, enabled=True)
        db.session.add(admin)
        db.session.commit()
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session.update(admin_id=admin.id, admin_username=admin.username,
                                   role=admin.role, is_superadmin=True)

    def _script(self, name):
        item = OnlineChatScript(name=name, script_code='<script>/*%s*/</script>' % name,
                                is_active=False)
        db.session.add(item)
        db.session.commit()
        return item

    def _state(self):
        return {row['name']: row['is_active']
                for row in self.client.get('/api/online-chat-scripts').get_json()}

    def test_activating_one_script_deactivates_the_others(self):
        first, second = self._script('first'), self._script('second')
        self.assertTrue(self.client.post(
            '/api/online-chat-scripts/%d/activate' % first.id).get_json()['success'])
        self.assertEqual(self._state(), {'first': True, 'second': False})
        self.assertTrue(self.client.post(
            '/api/online-chat-scripts/%d/activate' % second.id).get_json()['success'])
        self.assertEqual(self._state(), {'first': False, 'second': True})

    def test_the_active_script_can_be_disabled_again(self):
        item = self._script('only')
        self.client.post('/api/online-chat-scripts/%d/activate' % item.id)
        self.assertEqual(self._state(), {'only': True})

        response = self.client.post('/api/online-chat-scripts/%d/deactivate' % item.id)
        payload = response.get_json()
        self.assertEqual(response.status_code, 200, payload)
        self.assertTrue(payload['success'])
        self.assertFalse(payload['item']['is_active'])
        self.assertEqual(self._state(), {'only': False})
        # Nothing else may inherit the flag: "none active" is a valid state.
        self.assertFalse(OnlineChatScript.query.filter_by(is_active=True).count())

    def test_disabling_an_inactive_script_is_harmless(self):
        item = self._script('idle')
        response = self.client.post('/api/online-chat-scripts/%d/deactivate' % item.id)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertFalse(response.get_json()['item']['is_active'])

    def test_disabling_a_script_that_does_not_exist_is_a_404(self):
        response = self.client.post('/api/online-chat-scripts/999999/deactivate')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.get_json()['success'])

    def test_disabling_does_not_touch_the_code_or_the_other_rows(self):
        active = self._script('active')
        other = self._script('other')
        self.client.post('/api/online-chat-scripts/%d/activate' % active.id)
        self.client.post('/api/online-chat-scripts/%d/deactivate' % active.id)
        db.session.expire_all()
        self.assertEqual(db.session.get(OnlineChatScript, active.id).script_code,
                         '<script>/*active*/</script>')
        self.assertFalse(db.session.get(OnlineChatScript, other.id).is_active)


class OnlineChatWiringTests(unittest.TestCase):
    """The page has to offer the control, not only the API."""

    def setUp(self):
        self.template = TEMPLATE.read_text(encoding='utf-8').replace('\r\n', '\n')

    def test_the_active_card_offers_disable_instead_of_a_dead_button(self):
        self.assertIn('deactivateOnlineChatScript(${item.id})', self.template)
        self.assertIn("_t('Disable')", self.template)
        # The old markup rendered a disabled "Active" button, which is what made the
        # state look like a dead end.
        self.assertNotIn(
            '<button class="btn btn-success" style="padding: 6px 10px;" disabled>',
            self.template)

    def test_the_edit_modal_carries_the_same_control(self):
        self.assertIn('id="online-chat-deactivate-btn"', self.template)
        self.assertIn('deactivateOnlineChatScriptFromModal()', self.template)
        # Shown only for an active script, and hidden again when adding a new one.
        self.assertIn(".classList.toggle('hidden', !item.is_active)", self.template)
        self.assertIn("getElementById('online-chat-deactivate-btn').classList.add('hidden')",
                      self.template)

    def test_the_client_calls_the_endpoint_that_exists(self):
        self.assertIn('/api/online-chat-scripts/${id}/deactivate', self.template)
        source = (REPO / 'panel' / 'routes' / 'content.py').read_text(encoding='utf-8')
        self.assertIn("@bp.route('/api/online-chat-scripts/<int:item_id>/deactivate'",
                      source)


if __name__ == '__main__':
    unittest.main()
