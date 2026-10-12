"""Account identity, quota policy and panel traffic contracts (no network)."""
import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from panel.core.account_stats import aggregate_accounts
from panel.core import snapshot_model

NOW = 1_800_000_000_000
GB = 1024 ** 3
UUID = '4ce7db6e-4576-4e55-bb2a-452487fe1bb6'


def rows(modern=True, memberships=10, **changes):
    client = dict(id=UUID, email='account@example.test', totalGB=20 * GB,
                  up=GB, down=2 * GB, expiryTimestamp=-31 * 86400000,
                  expiryType='start_after_use', enable=True, global_enable=True,
                  is_online=True, traffic_available=True,
                  traffic_scope='account' if modern else 'membership')
    client.update(changes)
    return [dict(id=i, server_id=1, enable=True,
                 client_api_generation='v3' if modern else 'legacy',
                 clients=[copy.deepcopy(client)]) for i in range(memberships)]


class AccountStatsTests(unittest.TestCase):
    def aggregate(self, data):
        return aggregate_accounts(data, now_ms=NOW)

    def test_legacy_same_email_on_independent_inbounds_counts_separately(self):
        stats = self.aggregate(rows(False, 10))
        self.assertEqual(stats['total_clients'], 10)
        self.assertEqual(stats['download_raw'], 20 * GB)

    def test_legacy_single_client(self):
        self.assertEqual(self.aggregate(rows(False, 1))['total_clients'], 1)

    def test_v3_ten_memberships_are_one_account_with_one_allocation(self):
        stats = self.aggregate(rows())
        self.assertEqual(stats['total_clients'], 1)
        self.assertEqual(stats['inbound_memberships'], 10)
        self.assertEqual(stats['allocated_quota_raw'], 20 * GB)
        self.assertEqual(stats['limited_clients'], 1)
        self.assertEqual(stats['remaining_raw'], 17 * GB)

    def test_missing_uuid_stable_email(self):
        self.assertEqual(self.aggregate(rows(id=''))['total_clients'], 1)

    def test_uuid_only_identity(self):
        self.assertEqual(self.aggregate(rows(email=''))['total_clients'], 1)

    def test_email_conflict_is_reported_without_multiplying_quota(self):
        data = rows()
        data[1]['clients'][0]['id'] = 'another-id'
        stats = self.aggregate(data)
        self.assertEqual(stats['total_clients'], 1)
        self.assertEqual(stats['identity_conflicts'], 1)
        self.assertEqual(stats['allocated_quota_raw'], 20 * GB)

    def test_same_uuid_different_emails_is_reported(self):
        data = rows(memberships=2)
        data[1]['clients'][0]['email'] = 'other@example.test'
        stats = self.aggregate(data)
        self.assertEqual(stats['total_clients'], 2)
        self.assertEqual(stats['identity_conflicts'], 2)

    def test_two_servers_never_merge_email(self):
        data = rows(memberships=2)
        data[1]['server_id'] = 2
        self.assertEqual(self.aggregate(data)['total_clients'], 2)

    def test_account_statuses_are_counted_once(self):
        stats = self.aggregate(rows(totalGB=0))
        for field in ('not_started_clients', 'unlimited_volume_clients', 'online_clients', 'active_clients'):
            with self.subTest(field=field):
                self.assertEqual(stats[field], 1)
        self.assertEqual(stats['inactive_clients'], 0)

    def test_unlimited_expiry_once(self):
        self.assertEqual(self.aggregate(rows(expiryTimestamp=0, expiryType='unlimited'))['unlimited_expiry_clients'], 1)

    def test_global_disable_is_not_overridden_by_online_or_memberships(self):
        stats = self.aggregate(rows(global_enable=False))
        self.assertEqual(stats['active_clients'], 0)
        self.assertEqual(stats['inactive_clients'], 1)
        self.assertEqual(stats['remaining_raw'], 0)
        self.assertEqual(stats['unused_allocated_raw'], 17 * GB)

    def test_expired_accounts_do_not_inflate_usable_quota(self):
        stats = self.aggregate(rows(expiryTimestamp=NOW - 1, expiryType='expired'))
        self.assertEqual(stats['remaining_raw'], 0)
        self.assertEqual(stats['inactive_clients'], 1)
        self.assertEqual(stats['unused_allocated_raw'], 17 * GB)

    def test_expiry_boundary_is_unusable(self):
        self.assertEqual(self.aggregate(rows(expiryTimestamp=NOW))['remaining_raw'], 0)

    def test_all_disabled_memberships_are_unusable(self):
        data = rows(enable=False)
        self.assertEqual(self.aggregate(data)['remaining_raw'], 0)
        data[3]['clients'][0]['enable'] = True
        self.assertEqual(self.aggregate(data)['remaining_raw'], 17 * GB)

    def test_disabled_inbounds_are_unusable(self):
        data = rows()
        for inbound in data:
            inbound['enable'] = False
        self.assertEqual(self.aggregate(data)['remaining_raw'], 0)

    def test_replicated_account_traffic_does_not_multiply(self):
        stats = self.aggregate(rows())
        self.assertEqual((stats['upload_raw'], stats['download_raw']), (GB, 2 * GB))

    def test_distinct_explicit_membership_traffic_is_summed(self):
        data = rows(memberships=2, traffic_scope='membership')
        data[1]['clients'][0]['up'] = 2 * GB
        stats = self.aggregate(data)
        self.assertEqual(stats['upload_raw'], 3 * GB)
        self.assertEqual(stats['remaining_raw'], 13 * GB)

    def test_ambiguous_account_traffic_is_not_first_max_or_sum(self):
        data = rows()
        data[4]['clients'][0]['up'] = 10 * GB
        stats = self.aggregate(data)
        self.assertEqual(stats['traffic_incomplete_clients'], 1)
        self.assertEqual(stats['upload_raw'], 0)
        self.assertEqual(stats['remaining_raw'], 0)

    def test_missing_client_stats_is_not_fabricated_zero_usage(self):
        stats = self.aggregate(rows(traffic_available=False))
        self.assertEqual(stats['traffic_incomplete_clients'], 1)
        self.assertEqual(stats['remaining_raw'], 0)

    def test_missing_copy_can_use_known_account_counter(self):
        data = rows()
        data[0]['clients'][0]['traffic_available'] = False
        self.assertEqual(self.aggregate(data)['traffic_incomplete_clients'], 0)

    def test_normalization_round_trip_preserves_account_stats(self):
        data = rows()
        before = self.aggregate(data)
        retained, block = snapshot_model.normalize_retained_block(data, 1)
        self.assertEqual(len(block['clients']), 1)
        self.assertEqual(self.aggregate(retained), before)
        self.assertEqual(self.aggregate(snapshot_model.materialize_server_block(block)), before)

    def test_authorization_filter_precedes_aggregation_without_mutating_store(self):
        data = rows()
        before = copy.deepcopy(data)
        visible = [dict(inbound, clients=[c for c in inbound['clients'] if c['email'] == 'account@example.test'])
                   for inbound in data[:5]]
        self.assertEqual(self.aggregate(visible)['total_clients'], 1)
        self.assertEqual(self.aggregate(visible)['inbound_memberships'], 5)
        self.assertEqual(data, before)

    def test_renewal_and_reset_keep_account_count_stable(self):
        data = rows()
        for inbound in data:
            client = inbound['clients'][0]
            client.update(totalGB=50 * GB, up=0, down=0)
        stats = self.aggregate(data)
        self.assertEqual(stats['total_clients'], 1)
        self.assertEqual(stats['remaining_raw'], 50 * GB)

    def test_assign_and_delete_memberships_do_not_change_allocation(self):
        data = rows()
        for length in (1, 5, 10):
            with self.subTest(memberships=length):
                self.assertEqual(self.aggregate(data[:length])['allocated_quota_raw'], 20 * GB)
        self.assertEqual(self.aggregate([])['allocated_quota_raw'], 0)

    def test_mixed_fleet_has_account_and_legacy_client_units(self):
        modern = rows()
        legacy = rows(False, 10)
        for inbound in legacy:
            inbound['server_id'] = 2
        self.assertEqual(self.aggregate(modern + legacy)['total_clients'], 11)

    def test_unknown_capability_preserves_legacy_identity_and_reports_unknown(self):
        data = rows(False, 2)
        for inbound in data:
            inbound['client_api_generation'] = 'unknown'
        stats = self.aggregate(data)
        self.assertEqual(stats['total_clients'], 2)
        self.assertEqual(stats['unknown_capability_servers'], 1)

    def test_large_account_membership_set(self):
        data = rows(memberships=10)
        for inbound in data:
            inbound['clients'] = [dict(inbound['clients'][0], id=str(i), email='account%d@example.test' % i)
                                  for i in range(2000)]
        stats = self.aggregate(data)
        self.assertEqual(stats['total_clients'], 2000)
        self.assertEqual(stats['inbound_memberships'], 20000)
        self.assertEqual(stats['remaining_raw'], 2000 * 17 * GB)

    def test_token_presence_is_not_v3_capability(self):
        from panel.adapters import xui
        with mock.patch.dict(xui.XUI_CAPABILITY_CACHE, {}, clear=True):
            server = SimpleNamespace(id=99901, api_token='configured')
            self.assertFalse(xui.server_is_v3(server))
            self.assertEqual(xui.client_api_generation(server), 'unknown')
