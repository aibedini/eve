"""Phase 31 tests: the release readiness check."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "release_check.py")
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import release_check as rc  # noqa: E402


class VersionCheckTests(unittest.TestCase):
    def test_a_missing_version_fails(self):
        version, check = rc.check_app_version("APP_VERSION = \"x\"")
        self.assertIsNone(version)
        self.assertFalse(check["ok"])

    def test_a_semver_version_is_accepted(self):
        version, check = rc.check_app_version('APP_VERSION = "2.5.144"')
        self.assertEqual(version, "2.5.144")
        self.assertTrue(check["ok"])

    def test_release_profile_requires_the_changelog_to_match(self):
        changelog = "# Changelog\n\n## [2.5.85] - 2026-08-26\n"
        notes = "# Notes\n\n## [2.5.0]\n"
        strict = rc.check_version_docs("2.5.144", changelog, notes, "release")
        self.assertFalse(strict["ok"])
        self.assertTrue(strict["required"])
        matching = rc.check_version_docs("2.5.85", changelog,
                                         "# Notes\n\n## [2.5.85]\n", "release")
        self.assertTrue(matching["ok"], matching)

    def test_ci_profile_tolerates_a_lagging_changelog(self):
        changelog = "## [2.5.85] - 2026-08-26\n"
        check = rc.check_version_docs("2.5.144", changelog, "", "ci")
        self.assertTrue(check["ok"])
        self.assertFalse(check["required"])


class DependencyCheckTests(unittest.TestCase):
    def test_an_entry_without_a_hash_fails(self):
        lock = "flask==3.1.2 \\\n    --hash=sha256:aa\ncelery==5.4.0\n"
        check = rc.check_lockfile(lock)
        self.assertFalse(check["ok"])
        self.assertIn("celery", check["detail"])

    def test_a_fully_hashed_lock_is_accepted(self):
        lock = ("flask==3.1.2 \\\n    --hash=sha256:aa \\\n    --hash=sha256:bb\n"
                "celery==5.4.0 \\\n    --hash=sha256:cc\n")
        check = rc.check_lockfile(lock)
        self.assertTrue(check["ok"], check)
        self.assertIn("2 hash-pinned", check["detail"])

    def test_an_empty_lock_fails(self):
        self.assertFalse(rc.check_lockfile("")["ok"])

    def test_ranges_are_reported_but_not_fatal(self):
        check = rc.check_requirements("flask>=3.1.2\nflask-compress>=1.14\n")
        self.assertTrue(check["ok"])
        self.assertFalse(check["required"])
        self.assertIn("range", check["detail"])


class WorkflowAndImageCheckTests(unittest.TestCase):
    def test_missing_workflows_fail(self):
        check = rc.check_workflows({"security.yml": "trivy"})
        self.assertFalse(check["ok"])

    def test_a_missing_scanner_fails(self):
        files = {name: "gitleaks-action github/codeql-action gh-action-pip-audit"
                 " forbidden-artifacts"
                 for name in rc.REQUIRED_WORKFLOWS}
        check = rc.check_workflows(files)
        self.assertFalse(check["ok"])
        self.assertIn("trivy", check["detail"])

    def test_a_complete_workflow_set_passes(self):
        security = ("github/codeql-action gitleaks-action gh-action-pip-audit trivy"
                    " forbidden-artifacts")
        files = {"security.yml": security, "tests.yml": "pytest",
                 "docker-publish.yml": "sbom: true\nprovenance: true\n"}
        self.assertTrue(rc.check_workflows(files)["ok"])

    def test_the_dockerfile_must_be_non_root_and_hash_pinned(self):
        self.assertFalse(rc.check_dockerfile("FROM python:3.11\n")["ok"])
        root = "FROM python:3.11-slim\nUSER root\n--require-hashes\n"
        self.assertFalse(rc.check_dockerfile(root)["ok"])
        no_hash = "FROM python:3.11-slim\nUSER eve\n"
        self.assertFalse(rc.check_dockerfile(no_hash)["ok"])
        latest = "FROM python:3.11-slim\nUSER eve\n--require-hashes\n"
        self.assertTrue(rc.check_dockerfile(latest)["ok"])
        self.assertFalse(
            rc.check_dockerfile("FROM python:latest\nUSER eve\n--require-hashes\n")["ok"])

    def test_the_policy_must_describe_private_disclosure(self):
        self.assertFalse(rc.check_security_policy("")["ok"])
        self.assertFalse(rc.check_security_policy("# Security\n")["ok"])
        good = "Report vulnerabilities privately. Do not open a public issue."
        self.assertTrue(rc.check_security_policy(good)["ok"])

    def test_attestations_are_required(self):
        self.assertFalse(rc.check_attestations("sbom: true")["ok"])
        self.assertTrue(rc.check_attestations(
            "sbom: true\nprovenance: true\n")["ok"])

    def test_a_tracked_secret_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
            with open(os.path.join(tmp, "server.key"), "w", encoding="utf-8") as handle:
                handle.write("not a real key")
            subprocess.run(["git", "add", "server.key"], cwd=tmp, check=True)
            check = rc.check_tracked_files(tmp)
            self.assertFalse(check["ok"])
            self.assertIn("server.key", check["detail"])

    def test_the_example_env_file_is_allowed(self):
        for path in (".env", "instance/servers.db", "certs/panel.pem",
                     "deploy/id_rsa", "data/eve.sqlite3", "server.key",
                     "certs/client.pfx"):
            self.assertTrue(rc.is_secret_path(path), path)
        for path in (".env.docker.example", ".env.example", "app.py",
                     "docs/security/UPLOADS.md", "tests/test_x.py",
                     "static/logo.png"):
            self.assertFalse(rc.is_secret_path(path), path)


class ReleaseGuardIntegrationTests(unittest.TestCase):
    def test_the_real_tree_passes_the_ci_profile(self):
        result = subprocess.run(
            [sys.executable, SCRIPT, "--json"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(report["profile"], "ci")
        self.assertTrue(report["ok"], report["failed"])
        self.assertEqual(report["failed"], [])

    def test_the_publish_workflow_gates_on_the_guard_and_attests(self):
        with open(os.path.join(REPO_ROOT, ".github", "workflows",
                               "docker-publish.yml"), encoding="utf-8") as handle:
            workflow = handle.read()
        self.assertIn("scripts/release_check.py", workflow)
        self.assertIn("needs: [release-guard]", workflow)
        self.assertIn("sbom: true", workflow)
        self.assertIn("provenance: true", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("id-token: write", workflow)


if __name__ == "__main__":
    unittest.main()
