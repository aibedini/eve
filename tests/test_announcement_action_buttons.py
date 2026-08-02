import json
import unittest
from unittest.mock import patch

from panel.models import Announcement
from panel.routes.content import _parse_announcement_payload


class AnnouncementActionButtonTests(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            'message': '<p>Maintenance</p>',
            'start_at': '2026-08-02T10:00:00',
            'end_at': '2026-08-03T10:00:00',
            'targets': '*',
            'action_buttons': [
                {'title': 'Status', 'url': 'https://status.example.com', 'palette': 'green'},
                {'title': 'Support', 'url': '/support', 'palette': 'blue'},
            ],
            'button_columns': 2,
        }
        payload.update(overrides)
        return payload

    @patch('app.parse_iso_datetime', side_effect=lambda value: __import__('datetime').datetime.fromisoformat(value))
    def test_payload_keeps_button_order_and_two_column_grid(self, _parse):
        payload, error = _parse_announcement_payload(self._payload())

        self.assertIsNone(error)
        self.assertEqual(payload['button_columns'], 2)
        self.assertEqual([item['title'] for item in payload['action_buttons']], ['Status', 'Support'])

    @patch('app.parse_iso_datetime', side_effect=lambda value: __import__('datetime').datetime.fromisoformat(value))
    def test_payload_rejects_unsafe_button_url(self, _parse):
        payload, error = _parse_announcement_payload(self._payload(action_buttons=[{
            'title': 'Unsafe', 'url': 'javascript:alert(1)', 'palette': 'red',
        }]))

        self.assertIsNone(payload)
        self.assertIn('valid HTTP(S)', error)

    @patch('app.parse_iso_datetime', side_effect=lambda value: __import__('datetime').datetime.fromisoformat(value))
    def test_payload_rejects_invalid_grid_size(self, _parse):
        payload, error = _parse_announcement_payload(self._payload(button_columns=3))

        self.assertIsNone(payload)
        self.assertIn('one or two columns', error)

    def test_model_serializes_buttons_and_defaults_legacy_grid_to_one_column(self):
        buttons = [{'title': 'Docs', 'url': 'https://example.com/docs', 'palette': 'purple'}]
        announcement = Announcement(
            message='Hello',
            action_buttons=json.dumps(buttons),
            button_columns=None,
        )

        serialized = announcement.to_dict()
        self.assertEqual(serialized['action_buttons'], buttons)
        self.assertEqual(serialized['button_columns'], 1)


if __name__ == '__main__':
    unittest.main()
