"""Source-level regressions for the browser recovery and package-cache contract.

These intentionally avoid a browser, network, Redis and sleeps. The behavioral
backend paths have their own focused suites; these assertions protect the small JS
contract that previously regressed to a second mutation or an unconditional refetch.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
PACKAGES = (ROOT / "panel" / "routes" / "packages.py").read_text(encoding="utf-8")


class RenewalRecoveryContractTests(unittest.TestCase):
    def test_warm_package_cache_coalesces_requests_and_revalidates(self):
        self.assertIn("renewPackageCache.inflight", DASHBOARD)
        self.assertIn("If-None-Match", DASHBOARD)
        self.assertIn("res.status === 304", DASHBOARD)
        self.assertNotIn("/api/packages?purpose=renew&_=${Date.now()}", DASHBOARD)

    def test_package_response_is_private_and_revisioned(self):
        self.assertIn("private, max-age=60, stale-while-revalidate=120", PACKAGES)
        self.assertIn("X-Package-Revision", PACKAGES)
        self.assertIn("f\"{user.id}:{user.role}:{purpose}:{canonical}\"", PACKAGES)

    def test_lost_post_response_polls_same_operation_read_only(self):
        submit = DASHBOARD.split("async function submitRenewal() {", 1)[1]
        self.assertIn("operation_id: payload.operation_id", submit)
        self.assertIn("observeRenewalOperation();", submit)
        observer = DASHBOARD.split("function observeRenewalOperation() {", 1)[1]
        observer = observer.split("// renew-result-modal:end", 1)[0]
        self.assertIn("/renew/verify", observer)
        self.assertNotIn("/renew`", observer)

    def test_verified_snapshot_has_no_local_expiry_or_quota_guess(self):
        submit = DASHBOARD.split("async function submitRenewal() {", 1)[1]
        self.assertNotIn("resolveFallbackRenewExpiry", submit)
        self.assertNotIn("resolveFallbackRenewTotal", submit)
        self.assertNotIn("patch.up = 0", submit)


if __name__ == "__main__":
    unittest.main()
