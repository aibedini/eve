"""Phase 32 tests: the documentation index stays complete and correct.

A documentation tree without a guard rots: new documents are never linked and
relative links break silently. These tests keep docs/README.md as the complete
entry point and every relative link resolvable.
"""
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(REPO_ROOT, "docs")
INDEX = os.path.join(DOCS, "README.md")
LINK_RE = re.compile(r"\]\(([^)]+)\)")
SKIP_PREFIXES = ("http://", "https://", "mailto:", "#", "tel:")
REQUIRED_RUNBOOK_TOPICS = (
    "/api/doctor",
    "/api/audit-log",
    "panel.services.retention",
    "scripts/benchmark_baseline.py",
    "scripts/loadtest.py",
    "scripts/release_check.py",
    "gh attestation verify",
    "INCIDENT_RESPONSE.md",
)


def _docs_files():
    found = []
    for dirpath, dirnames, filenames in os.walk(DOCS):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".md"):
                path = os.path.join(dirpath, name)
                found.append(os.path.relpath(path, DOCS).replace(os.sep, "/"))
    return found


def _links(text):
    for raw in LINK_RE.findall(text):
        target = raw.strip().split(" ")[0].split("#", 1)[0]
        if not target or target.startswith(SKIP_PREFIXES):
            continue
        yield target


class DocsIndexTests(unittest.TestCase):
    def test_every_document_is_linked_from_the_index(self):
        with open(INDEX, encoding="utf-8") as handle:
            index = handle.read()
        linked = set(_links(index))
        missing = [name for name in _docs_files()
                   if name != "README.md" and name not in linked]
        self.assertEqual(missing, [], "not listed in docs/README.md: %s" % missing)

    def test_index_links_resolve(self):
        with open(INDEX, encoding="utf-8") as handle:
            index = handle.read()
        for target in _links(index):
            resolved = os.path.normpath(os.path.join(DOCS, target))
            self.assertTrue(os.path.exists(resolved),
                            "docs/README.md links a missing path: %s" % target)

    def test_no_broken_relative_links_anywhere(self):
        broken = []
        for name in _docs_files():
            path = os.path.join(DOCS, name)
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            base = os.path.dirname(path)
            for target in _links(text):
                if os.path.exists(os.path.normpath(os.path.join(base, target))):
                    continue
                broken.append("%s -> %s" % (name, target))
        self.assertEqual(broken, [], "broken relative links: %s" % broken)

    def test_the_root_readme_links_the_documentation_index(self):
        with open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        self.assertIn("docs/README.md", readme)

    def test_the_runbook_covers_the_operational_surface(self):
        with open(os.path.join(DOCS, "OPERATIONS_RUNBOOK.md"),
                  encoding="utf-8") as handle:
            runbook = handle.read()
        for topic in REQUIRED_RUNBOOK_TOPICS:
            self.assertIn(topic, runbook, topic)


if __name__ == "__main__":
    unittest.main()
