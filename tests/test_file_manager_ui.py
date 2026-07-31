import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FileManagerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager_template = (ROOT / 'templates' / 'sub_manager.html').read_text(
            encoding='utf-8')
        cls.base_template = (ROOT / 'templates' / 'base.html').read_text(
            encoding='utf-8')

    def test_file_manager_has_filename_search_and_clear_controls(self):
        self.assertIn('id="fm-search"', self.manager_template)
        self.assertIn('oninput="setFmSearch(this.value)"', self.manager_template)
        self.assertIn('window.clearFmSearch = function ()', self.manager_template)
        self.assertIn("String(f.name || '').toLocaleLowerCase().includes(_searchQuery)",
                      self.manager_template)

    def test_shared_date_parser_accepts_unix_seconds_and_milliseconds(self):
        self.assertIn(
            'Math.abs(value) < 1000000000000 ? value * 1000 : value',
            self.base_template,
        )
        self.assertIn('EveDate.formatDate(iso', self.manager_template)


if __name__ == '__main__':
    unittest.main()
