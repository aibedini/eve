"""Phase 23 tests: the reseller refresh projection must not copy or mutate the
shared snapshot.
"""
import copy
import os
import tempfile
import unittest
from unittest import mock

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from app import Admin, GLOBAL_SERVER_DATA, Server, app, db  # noqa: E402
from panel.core import snapshot_delta as delta  # noqa: E402
from panel.models import ClientOwnership  # noqa: E402


def _client(inbound_id, index, up, down, email=None, uuid=None, online=False):
    return {
        "email": email or ("c%d-%d@test" % (inbound_id, index)),
        "id": uuid or ("uuid-%d-%d" % (inbound_id, index)),
        "up": up,
        "down": down,
        "enable": True,
        "is_online": online,
        "expiryType": "fixed",
        "totalGB_formatted": "10 GB",
    }


def _inbound(server_id, inbound_id, clients):
    return {
        "server_id": server_id,
        "id": inbound_id,
        "enable": True,
        "remark": "in %d/%d" % (server_id, inbound_id),
        "total_up": "1.0 GB",
        "total_down": "2.0 GB",
        "client_count": len(clients),
        "clients": clients,
    }


class ResellerProjectionTests(unittest.TestCase):
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
        self._saved_snapshot = {key: value for key, value in GLOBAL_SERVER_DATA.items()}
        self.addCleanup(self._restore_snapshot)
        for model in (ClientOwnership, Server, Admin):
            model.query.delete()
        db.session.commit()
        self.superadmin = Admin(username="proj-root", role="superadmin",
                                is_superadmin=True, enabled=True)
        self.superadmin.set_password("CorrectHorseBattery1!")
        self.reseller = Admin(username="proj-reseller", role="reseller", enabled=True,
                              allowed_servers="*")
        self.reseller.set_password("CorrectHorseBattery1!")
        db.session.add_all([self.superadmin, self.reseller])
        db.session.commit()
        self.server = Server(name="proj-server", host="https://proj.invalid",
                             username="u", password="p", panel_type="auto")
        db.session.add(self.server)
        db.session.commit()
        db.session.add(ClientOwnership(
            reseller_id=self.reseller.id, server_id=self.server.id, inbound_id=1,
            client_email="owned@test", client_uuid="uuid-owned"))
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
        sid = self.server.id
        GLOBAL_SERVER_DATA.clear()
        GLOBAL_SERVER_DATA.update({
            "last_update": "t1",
            "is_updating": False,
            "stats": {"total_clients": 4},
            "servers_status": [{"server_id": sid, "name": "proj"}],
            "inbounds": [
                _inbound(sid, 1, [
                    _client(1, 0, 100, 200, email="owned@test", uuid="uuid-owned"),
                    _client(1, 1, 300, 400),
                ]),
                _inbound(sid, 2, [_client(2, 0, 500, 600)]),
            ],
        })

    def test_reseller_view_filters_clients_without_touching_the_snapshot(self):
        self._seed_snapshot()
        snapshot_before = copy.deepcopy(GLOBAL_SERVER_DATA["inbounds"])
        self._login(self.reseller)
        response = self.client.get("/api/refresh")
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        self.assertEqual(body["sync"]["mode"], "full")
        inbounds = {item["id"]: item for item in body["inbounds"]}
        self.assertEqual(sorted(inbounds), [1, 2])
        self.assertEqual([c["email"] for c in inbounds[1]["clients"]], ["owned@test"])
        self.assertEqual(inbounds[1]["client_count"], 1)
        self.assertEqual(inbounds[1]["total_up"], "---")
        self.assertEqual(inbounds[1]["total_down"], "---")
        self.assertEqual(inbounds[2]["clients"], [])
        self.assertEqual(inbounds[2]["client_count"], 0)
        self.assertEqual(body["stats"]["total_clients"], 1)
        self.assertEqual(body["stats"]["upload_raw"], 100)
        self.assertEqual(body["stats"]["download_raw"], 200)
        self.assertEqual(body["server_count"], 1)
        # The shared snapshot is still exactly what it was: no injected keys, no
        # truncated client lists, no zeroed totals.
        self.assertEqual(GLOBAL_SERVER_DATA["inbounds"], snapshot_before)
        self.assertEqual(GLOBAL_SERVER_DATA["inbounds"][0]["total_up"], "1.0 GB")
        self.assertEqual(len(GLOBAL_SERVER_DATA["inbounds"][0]["clients"]), 2)

    def test_superadmin_view_is_unchanged_and_idempotent(self):
        self._seed_snapshot()
        self._login(self.superadmin)
        first = self.client.get("/api/refresh").get_json()
        self.assertEqual(first["sync"]["mode"], "full")
        self.assertEqual(len(first["inbounds"]), 2)
        self.assertEqual([len(item["clients"]) for item in first["inbounds"]], [2, 1])
        # The superadmin path deliberately enriches the shared snapshot in place
        # (ownership annotations), so it is compared against itself: a second call
        # must return the same full view, still with every client.
        enriched = copy.deepcopy(GLOBAL_SERVER_DATA["inbounds"])
        second = self.client.get("/api/refresh").get_json()
        self.assertEqual(second["sync"]["mode"], "full")
        self.assertEqual(second["inbounds"], first["inbounds"])
        self.assertEqual(GLOBAL_SERVER_DATA["inbounds"], enriched)
        self.assertEqual(len(GLOBAL_SERVER_DATA["inbounds"][0]["clients"]), 2)


if __name__ == "__main__":
    unittest.main()