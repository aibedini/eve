import json
import unittest

from panel.models import SubAppConfig
from panel.routes.content import _normalize_sub_app_buttons


class SubAppActionButtonTests(unittest.TestCase):
    def test_normalizer_keeps_unlimited_button_order_and_palette(self):
        raw = [
            {
                'title': f'v2rayNG build {index}',
                'url': f'https://downloads.example/v2rayng-{index}.apk',
                'palette': 'green' if index % 2 else 'blue',
                'icon': 'download-simple' if index == 1 else '',
            }
            for index in range(1, 16)
        ]

        normalized = _normalize_sub_app_buttons(raw)

        self.assertEqual(len(normalized), 15)
        self.assertEqual(normalized[0]['title'], 'v2rayNG build 1')
        self.assertEqual(normalized[0]['icon'], 'download-simple')
        self.assertEqual(normalized[-1]['title'], 'v2rayNG build 15')
        self.assertEqual(normalized[1]['palette'], 'blue')

    def test_normalizer_accepts_phosphor_icon_names(self):
        button = _normalize_sub_app_buttons([{
            'title': 'Telegram',
            'url': 'https://t.me/example',
            'palette': 'blue',
            'icon': 'ph-telegram-logo',
        }])[0]

        self.assertEqual(button['icon'], 'telegram-logo')

    def test_normalizer_rejects_invalid_icon_names(self):
        with self.assertRaises(ValueError):
            _normalize_sub_app_buttons([{
                'title': 'Bad icon',
                'url': 'https://example.com',
                'palette': 'red',
                'icon': 'x\" onclick=\"alert(1)',
            }])

    def test_normalizer_allows_panel_files(self):
        self.assertEqual(
            _normalize_sub_app_buttons([{
                'title': 'Uploaded APK',
                'url': '/uploads/apps/client.apk',
                'palette': 'primary',
            }])[0]['url'],
            '/uploads/apps/client.apk',
        )

    def test_normalizer_rejects_executable_and_protocol_relative_urls(self):
        for unsafe_url in ('javascript:alert(1)', '//evil.example/client.apk'):
            with self.subTest(url=unsafe_url), self.assertRaises(ValueError):
                _normalize_sub_app_buttons([{
                    'title': 'Unsafe',
                    'url': unsafe_url,
                    'palette': 'red',
                }])

    def test_legacy_links_are_exposed_as_action_buttons(self):
        app_config = SubAppConfig(
            app_code='legacy',
            download_link='https://downloads.example/client.apk',
            store_link='https://store.example/client',
            tutorial_link='https://video.example/guide',
        )

        buttons = app_config.to_dict()['action_buttons']

        self.assertEqual([item['label_key'] for item in buttons], [
            'download', 'store', 'tutorial',
        ])

    def test_explicit_empty_list_does_not_restore_legacy_links(self):
        app_config = SubAppConfig(
            app_code='empty-actions',
            download_link='https://downloads.example/old.apk',
            action_buttons='[]',
        )

        self.assertEqual(app_config.to_dict()['action_buttons'], [])

    def test_custom_buttons_round_trip(self):
        expected = [{
            'title': 'ARM64 version',
            'url': 'https://downloads.example/arm64.apk',
            'palette': 'cyan',
            'icon': 'download-simple',
        }]
        app_config = SubAppConfig(
            app_code='custom-actions',
            action_buttons=json.dumps(expected),
        )

        self.assertEqual(app_config.to_dict()['action_buttons'], expected)


if __name__ == '__main__':
    unittest.main()
