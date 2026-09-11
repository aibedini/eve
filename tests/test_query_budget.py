"""Phase 20 tests: hot-path query budget (no N+1) and the settings memo.

The finance list endpoints used to issue one SELECT of system_settings per
rendered row (the identity map is weak, and the helper dropped its reference)
plus three lazy relationship loads per row. These tests pin the statement budget
so the N+1 cannot come back unnoticed.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

_DB_FILE = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_DB_FILE.close()
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _DB_FILE.name.replace(os.sep, "/"))
os.environ["FLASK_ENV"] = "development"
os.environ["DISABLE_BACKGROUND_THREADS"] = "1"

from sqlalchemy import event, inspect  # noqa: E402

from app import (  # noqa: E402
    _current_db_session,
    _get_or_create_system_setting,
    _settings_memo,
    app,
    db,
)
from panel.models import (  # noqa: E402
    Admin, BankCard, ManualReceipt, Payment, Server, SystemSetting, Transaction,
)


class SqlCounter:
    """Count statements executed inside the block (against this engine)."""

    def __init__(self):
        self.statements = []
        self._handler = None

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(" ".join(str(statement).split()))

    def __enter__(self):
        event.listen(db.engine, "before_cursor_execute", self._before)
        return self

    def __exit__(self, *exc):
        event.remove(db.engine, "before_cursor_execute", self._before)
        return False

    def count(self, prefix):
        return sum(1 for statement in self.statements if statement.startswith(prefix))


class SettingsMemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        cls.ctx.pop()

    def setUp(self):
        db.session.query(SystemSetting).delete()
        db.session.commit()

    def test_repeated_reads_do_not_re_select(self):
        with SqlCounter() as counter:
            values = [_get_or_create_system_setting("general_calendar", "jalali")
                      for _ in range(5)]
        self.assertEqual(set(values), {"jalali"})
        self.assertLessEqual(counter.count("SELECT system_settings"), 2)

    def test_commit_clears_the_memo_so_a_written_value_is_visible(self):
        self.assertEqual(_get_or_create_system_setting("general_calendar", "jalali"), "jalali")
        row = db.session.get(SystemSetting, "general_calendar")
        row.value = "gregorian"
        db.session.commit()
        self.assertEqual(_get_or_create_system_setting("general_calendar", "jalali"), "gregorian")

    def test_creating_a_default_does_not_expire_loaded_rows(self):
        admin = Admin(username="memo-admin", role="admin", enabled=True)
        admin.set_password("CorrectHorseBattery1!")
        db.session.add(admin)
        db.session.commit()
        _ = admin.username  # re-load after our own commit
        self.assertFalse(inspect(admin).expired)
        _get_or_create_system_setting("brand_new_setting", "value")
        self.assertFalse(inspect(admin).expired)
        self.assertEqual(
            db.session.get(SystemSetting, "brand_new_setting").value, "value")

    def test_missing_setting_without_a_default_is_not_created(self):
        self.assertIsNone(_get_or_create_system_setting("no_such_setting"))
        self.assertIsNone(db.session.get(SystemSetting, "no_such_setting"))

    def test_memo_does_not_survive_a_new_session(self):
        _get_or_create_system_setting("general_calendar", "jalali")
        self.assertIn("general_calendar", _settings_memo())
        db.session.remove()
        db.session.get(SystemSetting, "general_calendar")
        self.assertNotIn("general_calendar", _settings_memo())

    def test_current_db_session_returns_a_real_session(self):
        session = _current_db_session()
        self.assertTrue(hasattr(session, "expire_on_commit"))
        self.assertIs(session, db.session())


class FinanceQueryBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = app.app_context()
        cls.ctx.push()
        db.create_all()
        now = datetime.utcnow()
        cls.admin = Admin(username="budget-admin", role="admin", enabled=True)
        cls.admin.set_password("CorrectHorseBattery1!")
        cls.reviewer = Admin(username="budget-reviewer", role="admin", enabled=True)
        cls.reviewer.set_password("CorrectHorseBattery1!")
        cls.server = Server(name="budget-server", host="https://budget.invalid",
                            username="u", password="p", panel_type="auto", enabled=True)
        cls.card = BankCard(label="budget-card", card_number="6037997512345678",
                            is_active=True)
        db.session.add_all([cls.admin, cls.reviewer, cls.server, cls.card])
        db.session.commit()
        transactions = []
        for index in range(25):
            transactions.append(Transaction(
                admin_id=cls.admin.id, server_id=cls.server.id, card_id=cls.card.id,
                amount=1000 + index, type="purchase", category="income",
                client_email="budget%d@example.test" % index,
                description="budget row %d" % index,
                created_at=now - timedelta(minutes=index),
            ))
        db.session.add_all(transactions)
        payments = []
        for index in range(12):
            payments.append(Payment(
                admin_id=cls.admin.id, card_id=cls.card.id, amount=5000 + index,
                payment_date=now - timedelta(minutes=index),
                client_email="budget%d@example.test" % index,
                sender_name="Payer %d" % index,
            ))
        db.session.add_all(payments)
        receipts = []
        for index in range(6):
            receipts.append(ManualReceipt(
                admin_id=cls.admin.id, card_id=cls.card.id, amount=7000 + index,
                deposit_at=now - timedelta(minutes=index),
                reference_code="REF-%d" % index, status="pending",
                reviewer_id=cls.reviewer.id,
            ))
        db.session.add_all(receipts)
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

    def test_transactions_page_stays_within_its_query_budget(self):
        with SqlCounter() as counter:
            response = self.client.get("/api/transactions?limit=20")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.get_json()["transactions"]), 20)
        self.assertLessEqual(counter.count("SELECT system_settings"), 4)
        self.assertLessEqual(counter.count("SELECT servers"), 2)
        self.assertLessEqual(counter.count("SELECT bank_cards"), 3)
        self.assertLessEqual(len(counter.statements), 14, counter.statements)

    def test_payments_page_stays_within_its_query_budget(self):
        with SqlCounter() as counter:
            response = self.client.get("/api/payments?limit=20")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertLessEqual(counter.count("SELECT system_settings"), 4)
        self.assertLessEqual(counter.count("SELECT admins"), 4)
        self.assertLessEqual(len(counter.statements), 18, counter.statements)

    def test_receipt_list_does_not_reload_the_reviewer_per_row(self):
        with SqlCounter() as counter:
            response = self.client.get("/api/payments?limit=20&type=receipt")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertLessEqual(counter.count("SELECT admins"), 4)


if __name__ == "__main__":
    unittest.main()
