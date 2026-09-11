"""Phase 22 tests: server-bounded list endpoints and the pagination contract."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from sqlalchemy import event  # noqa: E402

from app import Admin, app, db  # noqa: E402
from panel.models import BnqoAgent, BnqoIncident, BnqoLink  # noqa: E402
from panel.routes.common import (  # noqa: E402
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    page_meta,
    page_params,
)


class PageHelperTests(unittest.TestCase):
    def test_defaults_are_used_when_nothing_is_asked(self):
        with app.test_request_context("/api/x"):
            self.assertEqual(page_params(), (DEFAULT_PAGE_SIZE, 0))

    def test_limit_and_offset_are_honoured(self):
        with app.test_request_context("/api/x?limit=25&offset=75"):
            self.assertEqual(page_params(), (25, 75))

    def test_oversized_limit_is_clamped_to_the_server_maximum(self):
        with app.test_request_context("/api/x?limit=999999"):
            self.assertEqual(page_params()[0], MAX_PAGE_SIZE)
        with app.test_request_context("/api/x?limit=100000"):
            self.assertEqual(page_params(maximum=250)[0], 250)

    def test_garbage_and_negative_values_fall_back_safely(self):
        with app.test_request_context("/api/x?limit=abc&offset=xyz"):
            self.assertEqual(page_params(), (DEFAULT_PAGE_SIZE, 0))
        with app.test_request_context("/api/x?limit=0&offset=-5"):
            self.assertEqual(page_params(), (1, 0))

    def test_page_per_page_alias_computes_the_offset(self):
        with app.test_request_context("/api/x?page=3&per_page=10"):
            self.assertEqual(page_params(), (10, 20))
        with app.test_request_context("/api/x?page=0"):
            self.assertEqual(page_params(default=50), (50, 0))

    def test_meta_reports_the_next_page_and_the_row_count(self):
        self.assertEqual(page_meta(250, 100, 100), {
            "total": 250, "limit": 100, "offset": 100, "count": 100,
            "has_more": True, "next_offset": 200,
        })
        self.assertEqual(page_meta(50, 100, 0)["has_more"], False)
        self.assertIsNone(page_meta(50, 100, 0)["next_offset"])
        self.assertEqual(page_meta(50, 100, 0)["count"], 50)
        self.assertEqual(page_meta(0, 100, 0)["count"], 0)


class BnqoPaginationTests(unittest.TestCase):
    LINKS = 750

    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        now = datetime.utcnow()
        cls.admin = Admin(username="page-admin", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        db.session.add(cls.admin)
        agents = [
            BnqoAgent(name="page-agent-a", role="iran", token="t" * 32,
                      pubkey="p" * 32, address="10.0.0.1", port=9000),
            BnqoAgent(name="page-agent-b", role="outside", token="u" * 32,
                      pubkey="q" * 32, address="10.0.0.2", port=9000),
        ]
        db.session.add_all(agents)
        db.session.commit()
        cls.a, cls.b = agents[0].id, agents[1].id
        db.session.bulk_save_objects([
            BnqoLink(name="page-link-%d" % index, agent_a_id=cls.a, agent_b_id=cls.b,
                     enabled=True, status="up")
            for index in range(cls.LINKS)
        ])
        db.session.commit()
        first_link = BnqoLink.query.order_by(BnqoLink.id.asc()).first()
        cls.link_id = first_link.id
        db.session.add_all([
            BnqoIncident(link_id=cls.link_id, kind="loss", status="open",
                         opened_at=now - timedelta(minutes=index))
            for index in range(3)
        ])
        db.session.commit()
        cls.client = app.test_client()
        with cls.client.session_transaction() as sess:
            sess.clear()
            sess["admin_id"] = cls.admin.id
            sess["role"] = "admin"
            sess["is_superadmin"] = False

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.ctx.pop()

    def test_links_default_page_is_bounded_and_reports_the_total(self):
        body = self.client.get("/api/bnqo/links").get_json()
        self.assertEqual(len(body["links"]), DEFAULT_PAGE_SIZE)
        self.assertEqual(body["total"], self.LINKS)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_offset"], DEFAULT_PAGE_SIZE)

    def test_links_offset_walks_to_the_last_page(self):
        body = self.client.get("/api/bnqo/links?limit=50&offset=700").get_json()
        self.assertEqual(len(body["links"]), 50)
        self.assertFalse(body["has_more"])
        self.assertIsNone(body["next_offset"])
        self.assertEqual(body["count"], 50)

    def test_links_limit_is_clamped_to_the_server_maximum(self):
        body = self.client.get("/api/bnqo/links?limit=999999").get_json()
        self.assertLessEqual(len(body["links"]), MAX_PAGE_SIZE)
        self.assertEqual(body["limit"], MAX_PAGE_SIZE)
        self.assertFalse(body["has_more"])

    def test_links_single_link_filter(self):
        body = self.client.get("/api/bnqo/links?link_id=%d" % self.link_id).get_json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["links"]), 1)
        self.assertEqual(body["links"][0]["id"], self.link_id)
        self.assertEqual(body["links"][0]["agent_a"]["id"], self.a)

    def test_links_page_does_not_load_agents_per_row(self):
        statements = []
        def _before(conn, cursor, statement, parameters, context, executemany):
            statements.append(" ".join(str(statement).split()))
        event.listen(db.engine, "before_cursor_execute", _before)
        try:
            response = self.client.get("/api/bnqo/links?limit=200")
        finally:
            event.remove(db.engine, "before_cursor_execute", _before)
        self.assertEqual(response.status_code, 200)
        agent_selects = sum(1 for s in statements if s.startswith("SELECT bnqo_agents"))
        self.assertLessEqual(agent_selects, 2, statements)
        self.assertLessEqual(len(statements), 8, statements)

    def test_agents_list_reports_its_total(self):
        body = self.client.get("/api/bnqo/agents").get_json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["agents"]), 2)
        self.assertFalse(body["has_more"])

    def test_incidents_keep_the_default_window_but_honour_limit(self):
        body = self.client.get("/api/bnqo/incidents").get_json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["incidents"]), 3)
        limited = self.client.get("/api/bnqo/incidents?limit=1").get_json()
        self.assertEqual(len(limited["incidents"]), 1)
        self.assertTrue(limited["has_more"])
        self.assertEqual(limited["next_offset"], 1)


if __name__ == "__main__":
    unittest.main()