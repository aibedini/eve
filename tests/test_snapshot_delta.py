"""Phase 11 tests: snapshot delta sync for /api/refresh."""
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import (  # noqa: E402
    Admin, ClientOwnership, GLOBAL_SERVER_DATA, Server, app, db,
)
from panel.core import snapshot_delta as delta  # noqa: E402
from panel.core import snapshot_model  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.lists = {}

    def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    def ltrim(self, key, start, stop):
        items = self.lists.get(key, [])
        self.lists[key] = items[start:stop + 1]
        return True

    def expire(self, key, ttl):
        return True

    def lrange(self, key, start, stop):
        return self.lists.get(key, [])[start:stop + 1]


def _inbound(server_id, inbound_id, up=0, clients=1):
    return {
        "server_id": server_id,
        "id": inbound_id,
        "remark": "in %d/%d" % (server_id, inbound_id),
        "clients": [
            {"email": "c%d-%d@test" % (inbound_id, index), "up": up, "enable": True,
             "id": "uuid-%d-%d" % (inbound_id, index)}
            for index in range(clients)
        ],
    }


def _snapshot(last_update="t1", up=0):
    return {
        "last_update": last_update,
        "inbounds": [_inbound(1, 1, up), _inbound(1, 2, up), _inbound(2, 1, up)],
        "servers_status": [{"server_id": 1}, {"server_id": 2}],
        "stats": {"total_clients": 3},
        "is_updating": False,
    }


class SnapshotDeltaTests(unittest.TestCase):
    def setUp(self):
        delta.reset_state()
        self._patch = mock.patch.object(delta, "_redis", return_value=None)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_first_sync_reports_every_inbound_once(self):
        result = delta.sync(_snapshot())
        self.assertEqual(result["revision"], 1)
        self.assertEqual(len(result["changed"]), 3)
        self.assertEqual(result["removed"], [])

    def test_unchanged_snapshot_keeps_the_revision_and_does_no_work(self):
        snapshot = _snapshot()
        first = delta.sync(snapshot)
        with mock.patch.object(delta, "fingerprint", side_effect=AssertionError("must not rehash")):
            second = delta.sync(snapshot)
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(second["changed"], [])

    def test_changed_inbound_is_reported_and_removed_is_tracked(self):
        snapshot = _snapshot()
        delta.sync(snapshot)
        snapshot["inbounds"][0]["clients"][0]["up"] = 42
        snapshot["last_update"] = "t2"
        result = delta.sync(snapshot)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["changed"], [[1, 1]])
        self.assertEqual(result["removed"], [])

        snapshot["inbounds"] = snapshot["inbounds"][1:]
        snapshot["last_update"] = "t3"
        result = delta.sync(snapshot)
        self.assertEqual(result["revision"], 3)
        self.assertEqual(result["removed"], [[1, 1]])
        self.assertEqual(result["changed"], [])

    def test_explicit_mark_dirty_rehashes_without_a_timestamp_change(self):
        snapshot = _snapshot()
        delta.sync(snapshot)
        snapshot["inbounds"][1]["clients"][0]["up"] = 7
        delta.mark_dirty()
        result = delta.sync(snapshot)
        self.assertEqual(result["changed"], [[1, 2]])

    def test_server_hint_limits_the_rehash(self):
        snapshot = _snapshot()
        delta.sync(snapshot)
        original = delta.fingerprint
        hashed = []

        def counting(inbound):
            hashed.append(delta.snapshot_key(inbound))
            return original(inbound)

        snapshot["inbounds"][0]["clients"][0]["up"] = 5
        snapshot["last_update"] = "t2"
        delta.mark_dirty(server_ids=[1])
        with mock.patch.object(delta, "fingerprint", side_effect=counting):
            result = delta.sync(snapshot)
        self.assertTrue(hashed)
        self.assertTrue(all(key[0] == 1 for key in hashed), hashed)
        self.assertEqual(result["changed"], [[1, 1]])

    def test_shared_v3_entity_marks_every_membership_inbound_changed(self):
        uid = '4ce7db6e-4576-4e55-bb2a-452487fe1bb6'
        expanded = [
            {'server_id': 7, 'id': 10,
             'clients': [{'id': uid, 'email': 'a@x', 'up': 1}]},
            {'server_id': 7, 'id': 20,
             'clients': [{'id': uid, 'email': 'a@x', 'up': 1}]},
        ]
        retained, _ = snapshot_model.normalize_retained_block(expanded, 7)
        snapshot = {'last_update': 't1', 'inbounds': retained,
                    'servers_status': [], 'stats': {}}
        delta.sync(snapshot)
        retained[0]['clients'][0]['up'] = 9
        snapshot['last_update'] = 't2'
        delta.mark_dirty(inbound_keys=[(7, 10), (7, 20)])
        with mock.patch.object(delta, 'fingerprint', wraps=delta.fingerprint) as fingerprints:
            result = delta.sync(snapshot)
        self.assertEqual(result['changed'], [[7, 10], [7, 20]])
        self.assertEqual(fingerprints.call_count, 2)

    def test_hinted_removal_is_detected(self):
        snapshot = _snapshot()
        delta.sync(snapshot)
        snapshot["inbounds"] = [row for row in snapshot["inbounds"]
                                if not (row["server_id"] == 2 and row["id"] == 1)]
        snapshot["last_update"] = "t2"
        delta.mark_dirty(server_ids=[2])
        result = delta.sync(snapshot)
        self.assertEqual(result["removed"], [[2, 1]])
        self.assertEqual(result["changed"], [])

    def test_metadata_only_change_produces_an_empty_delta(self):
        snapshot = _snapshot()
        revision = delta.build_sync(snapshot, None)["revision"]
        snapshot["servers_status"] = [{"server_id": 1, "reachable": False}]
        snapshot["last_update"] = "t2"
        result = delta.build_sync(snapshot, revision)
        self.assertEqual(result["mode"], "delta")
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["removed"], [])

    def test_build_sync_full_delta_unchanged(self):
        snapshot = _snapshot()
        full = delta.build_sync(snapshot, None)
        self.assertEqual(full["mode"], "full")
        self.assertEqual(full["reason"], "no_revision")
        revision = full["revision"]

        unchanged = delta.build_sync(snapshot, revision)
        self.assertEqual(unchanged["mode"], "unchanged")

        snapshot["inbounds"][2]["clients"][0]["up"] = 9
        snapshot["last_update"] = "t2"
        moved = delta.build_sync(snapshot, revision)
        self.assertEqual(moved["mode"], "delta")
        self.assertEqual(moved["changed"], [[2, 1]])
        self.assertEqual(moved["removed"], [])

    def test_unknown_or_future_revision_falls_back_to_full(self):
        snapshot = _snapshot()
        revision = delta.build_sync(snapshot, None)["revision"]
        self.assertEqual(delta.build_sync(snapshot, revision + 50)["mode"], "full")
        self.assertEqual(delta.build_sync(snapshot, revision + 50)["reason"],
                         "unknown_revision")
        self.assertEqual(delta.build_sync(snapshot, "not-a-number")["mode"], "full")
        self.assertEqual(delta.build_sync(snapshot, revision + 50)["mode"], "full")

    def test_history_gap_falls_back_to_full(self):
        snapshot = _snapshot()
        with mock.patch.object(delta, "MAX_HISTORY", 2):
            delta.reset_state()
            first = delta.sync(snapshot)["revision"]
            for index in range(5):
                snapshot["inbounds"][0]["clients"][0]["up"] = index + 1
                snapshot["last_update"] = "t%d" % (index + 2)
                delta.sync(snapshot)
            result = delta.build_sync(snapshot, first)
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["reason"], "history_gap")

    def test_too_many_changes_falls_back_to_full(self):
        snapshot = _snapshot()
        revision = delta.sync(snapshot)["revision"]
        snapshot["inbounds"][0]["clients"][0]["up"] = 1
        snapshot["inbounds"][1]["clients"][0]["up"] = 1
        snapshot["last_update"] = "t2"
        with mock.patch.object(delta, "MAX_DELTA_KEYS", 1):
            result = delta.build_sync(snapshot, revision)
        self.assertEqual(result["mode"], "full")
        self.assertEqual(result["reason"], "too_many_changes")

    def test_select_inbounds_returns_the_named_rows(self):
        snapshot = _snapshot()
        rows = delta.select_inbounds(snapshot, [[1, 2], [2, 1]])
        self.assertEqual([row["id"] for row in rows], [2, 1])
        self.assertEqual(delta.select_inbounds(snapshot, []), [])

    def test_redis_shares_revisions_and_history_across_workers(self):
        fake = FakeRedis()
        with mock.patch.object(delta, "_redis", return_value=fake):
            snapshot = _snapshot()
            self.assertEqual(delta.sync(snapshot)["revision"], 1)
            snapshot["inbounds"][0]["clients"][0]["up"] = 5
            snapshot["last_update"] = "t2"
            self.assertEqual(delta.sync(snapshot)["revision"], 2)
            pushed = [record["revision"] for record in delta._redis_history()]
            self.assertEqual(pushed, [2, 1])

            # A second worker starts with no local history but the same Redis.
            delta.reset_state()
            result = delta.build_sync(snapshot, 1)
        self.assertEqual(result["mode"], "delta")
        self.assertEqual(result["revision"], 3)


class RefreshRouteDeltaTests(unittest.TestCase):
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

    def setUp(self):
        delta.reset_state()
        self._redis_patch = mock.patch.object(delta, "_redis", return_value=None)
        self._redis_patch.start()
        self.addCleanup(self._redis_patch.stop)
        self._saved_snapshot = {
            key: value for key, value in GLOBAL_SERVER_DATA.items()
        }
        self.addCleanup(self._restore_snapshot)
        for model in (ClientOwnership, Server, Admin):
            model.query.delete()
        db.session.commit()
        self.superadmin = Admin(username="delta-root", role="superadmin",
                                is_superadmin=True, enabled=True)
        self.superadmin.set_password("CorrectHorseBattery1!")
        self.reseller = Admin(username="delta-reseller", role="reseller", enabled=True,
                              allowed_servers="[]")
        self.reseller.set_password("CorrectHorseBattery1!")
        db.session.add_all([self.superadmin, self.reseller])
        db.session.commit()
        self.server = Server(name="delta-one", host="https://delta.invalid", username="u",
                             password="p", panel_type="auto")
        db.session.add(self.server)
        db.session.commit()
        self.client = app.test_client()

    def _restore_snapshot(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update(self._saved_snapshot)

    def _login(self, admin):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = admin.id
            sess["role"] = admin.role
            sess["is_superadmin"] = bool(admin.is_superadmin)

    def _seed_snapshot(self):
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({
            "last_update": "t1",
            "is_updating": False,
            "stats": {"total_clients": 2},
            "servers_status": [{"server_id": self.server.id, "name": "one"}],
            "inbounds": [
                _inbound(self.server.id, 1, clients=2),
                _inbound(self.server.id, 2, clients=2),
            ],
        })

    def test_superadmin_full_then_unchanged_then_delta(self):
        self._seed_snapshot()
        self._login(self.superadmin)

        full = self.client.get("/api/refresh")
        self.assertEqual(full.status_code, 200)
        payload = full.get_json()
        self.assertEqual(payload["sync"]["mode"], "full")
        self.assertEqual(len(payload["inbounds"]), 2)
        revision = payload["sync"]["revision"]

        unchanged = self.client.get("/api/refresh?since=%d" % revision)
        body = unchanged.get_json()
        self.assertEqual(body["sync"]["mode"], "unchanged")
        self.assertNotIn("inbounds", body)
        self.assertLess(len(unchanged.get_data()), 600)

        GLOBAL_SERVER_DATA["inbounds"][0]["clients"][0]["up"] = 12345
        GLOBAL_SERVER_DATA["last_update"] = "t2"
        delta_reply = self.client.get("/api/refresh?since=%d" % revision).get_json()
        self.assertEqual(delta_reply["sync"]["mode"], "delta")
        self.assertEqual([row["id"] for row in delta_reply["inbounds"]], [1])
        self.assertEqual(delta_reply["removed"], [])

        GLOBAL_SERVER_DATA["inbounds"] = GLOBAL_SERVER_DATA["inbounds"][1:]
        GLOBAL_SERVER_DATA["last_update"] = "t3"
        removed = self.client.get(
            "/api/refresh?since=%d" % delta_reply["sync"]["revision"]).get_json()
        self.assertEqual(removed["sync"]["mode"], "delta")
        self.assertEqual(removed["removed"], [[self.server.id, 1]])

    def test_header_can_carry_the_revision_and_unknown_revision_is_full(self):
        self._seed_snapshot()
        self._login(self.superadmin)
        revision = self.client.get("/api/refresh").get_json()["sync"]["revision"]
        header = self.client.get("/api/refresh", headers={"X-Eve-Snapshot": str(revision)})
        self.assertEqual(header.get_json()["sync"]["mode"], "unchanged")
        unknown = self.client.get("/api/refresh?since=999999").get_json()
        self.assertEqual(unknown["sync"]["mode"], "full")
        self.assertEqual(unknown["sync"]["reason"], "unknown_revision")

    def test_reseller_unchanged_poll_is_cheap_and_full_is_filtered(self):
        self._seed_snapshot()
        for inbound_id in (1, 2):
            db.session.add(ClientOwnership(
                reseller_id=self.reseller.id, server_id=self.server.id,
                inbound_id=inbound_id, client_email="c%d-0@test" % inbound_id,
                client_uuid="uuid-%d-0" % inbound_id))
        db.session.commit()
        self._login(self.reseller)

        full = self.client.get("/api/refresh").get_json()
        self.assertEqual(full["sync"]["mode"], "full")
        self.assertEqual(full["sync"]["reason"], "reseller_view")
        self.assertEqual(len(full["inbounds"]), 2)
        revision = full["sync"]["revision"]

        unchanged = self.client.get("/api/refresh?since=%d" % revision)
        body = unchanged.get_json()
        self.assertEqual(body["sync"]["mode"], "unchanged")
        self.assertLess(len(unchanged.get_data()), 600)

        GLOBAL_SERVER_DATA["inbounds"][0]["clients"][0]["up"] = 1
        GLOBAL_SERVER_DATA["last_update"] = "t2"
        again = self.client.get("/api/refresh?since=%d" % revision).get_json()
        self.assertEqual(again["sync"]["mode"], "full")
        self.assertEqual(len(again["inbounds"]), 2)


if __name__ == "__main__":
    unittest.main()
