"""Regression checks for the native Eve CLI's HTTP health signal."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"
ENTRYPOINT = 'if [ "${1:-}" = "--install-xray" ]; then'


def bash_executable():
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and git_bash.exists():
        return str(git_bash)
    return shutil.which("bash") or str(git_bash)


class EveCliHealthTests(unittest.TestCase):
    def run_health(self, code, site_config="server_name eve.test;\nlisten 443 ssl;\n"):
        source = SETUP.read_text(encoding="utf-8")
        self.assertIn(ENTRYPOINT, source)
        definitions = source.split(ENTRYPOINT, 1)[0]
        harness = r'''
APP_DIR=/tmp
ENV_FILE=/nonexistent/eve.env
systemctl() {
    case "$1" in
        cat|is-active) return 0 ;;
        *) return 1 ;;
    esac
}
nginx() { return 0; }
curl() { printf '302'; }
require_root() { return 0; }
df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\nx 100 50 50 50%% /tmp\n'; }
probe_web_http() { printf '%s' "$TEST_HTTP_CODE"; [ "$TEST_HTTP_CODE" != 000 ]; }
get_web_status
if health_report; then echo HEALTH_EXIT=0; else echo HEALTH_EXIT=1; fi
'''
        with tempfile.TemporaryDirectory() as directory:
            site = Path(directory) / "eve-nginx-site.conf"
            site.write_text(site_config, encoding="utf-8")
            site_path = site.as_posix()
            if os.name == "nt":
                site_path = "/" + site.drive[0].lower() + site_path[2:]
            env = dict(
                os.environ, TEST_HTTP_CODE=code,
                EVE_TEST_PYTHON=sys.executable.replace("\\", "/"),
                EVE_NGINX_SITE_FILE=site_path,
            )
            return subprocess.run(
                [bash_executable()], input='python3() { "$EVE_TEST_PYTHON" "$@"; }\n' + definitions + harness, text=True,
                cwd=ROOT, env=env, capture_output=True, check=False, encoding="utf-8",
            )

    def test_active_service_but_unresponsive_app_is_reported(self):
        result = self.run_health("000")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("Not responding", result.stdout)
        self.assertIn("HTTP 000 (FAILED)", result.stdout)
        self.assertIn("HEALTH_EXIT=1", result.stdout)

    def test_redirect_from_app_is_healthy(self):
        result = self.run_health("302")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("Responding (HTTP 302)", result.stdout)
        self.assertIn("HTTP 302 (OK)", result.stdout)
        self.assertIn("HEALTH_EXIT=0", result.stdout)

    def test_nginx_site_without_domain_is_reported(self):
        result = self.run_health("302", site_config="listen 443 ssl;\n")
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn("no server_name configured", result.stdout)
        self.assertIn("HEALTH_EXIT=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
