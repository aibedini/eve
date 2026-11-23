import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from runpy import run_path
from unittest.mock import patch

_DB = tempfile.TemporaryDirectory()
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_DB.name, 'queue.db').replace(os.sep, '/')
os.environ['FLASK_ENV'] = 'development'
os.environ['DISABLE_BACKGROUND_THREADS'] = '1'
os.environ['EVE_SKIP_IMPORT_MIGRATIONS'] = '1'

from flask import jsonify, request, session  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402
from app import app, db  # noqa: E402
from panel.models import Admin, Server  # noqa: E402
from panel.services import client_mutation_queue as queue  # noqa: E402


class ClientMutationQueueTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.engine.dispose()
        _DB.cleanup()

    def setUp(self):
        with app.app_context():
            db.create_all()
            queue.Job.query.delete()
            if db.session.get(Server, 7) is None:
                db.session.add(Server(id=7, name='queue-test', host='https://example.invalid',
                                      username='unused', password='unused'))
            if not Admin.query.filter_by(username='queue-admin-0').first():
                db.session.add_all(Admin(username=f'queue-admin-{n}', password_hash='unused', role='superadmin',
                                         enabled=True, is_superadmin=True) for n in range(10))
            db.session.commit()
            self.actors = [a.id for a in Admin.query.filter(Admin.username.like('queue-admin-%'))
                           .order_by(Admin.id).all()]
        self.clients = [self.login(admin_id) for admin_id in self.actors]

    def login(self, admin_id):
        client = app.test_client()
        with client.session_transaction() as login:
            login['admin_id'] = admin_id
        return client

    def enqueue(self, number, email, *, actor=0, action='renew', payload=None):
        path = f'/api/client/7/1/{email}/{action}' if action != 'add' else '/api/client/7/1/add'
        return self.clients[actor].post(path, json=payload if payload is not None else {'days': 30},
                                        headers={'X-Eve-Client-Job': f'client-operation-{number}'})

    def test_ten_browsers_can_queue_and_run_different_accounts_together(self):
        for i in range(10):
            response = self.enqueue(i, f'user{i}', actor=i)
            self.assertEqual(response.status_code, 202, response.get_json())
        start = threading.Barrier(10, timeout=12)

        def renew(server_id, inbound_id, email):
            actor = session['admin_id']
            start.wait()
            return jsonify(success=True, email=email, actor=actor)

        with app.app_context(), patch.dict(app.view_functions, {'clients.renew_client': renew}):
            claims = []
            queue.xui_compat.resolve_compatibility(7, '3.11.0')
            try:
                with patch('panel.routes.clients._acquire_renew_lock', return_value='lock'):
                    with ThreadPoolExecutor(10) as workers:
                        for i in range(10):
                            claims.append(queue._claim_job(set(), {7} if i else set()))
                        self.assertTrue(all(claim and not claim[2] for claim in claims))
                        jobs = [workers.submit(queue._run_job, app, claim[0]) for claim in claims]
                        for job in jobs:
                            job.result(timeout=15)
            finally:
                queue.xui_compat.invalidate_compatibility(7)
        for i, client in enumerate(self.clients):
            result = client.get(f'/api/client-jobs/client-operation-{i}').get_json()
            self.assertEqual(result['result']['body'], {
                'success': True, 'email': f'user{i}', 'actor': self.actors[i],
            })
        denied = self.clients[1].get('/api/client-jobs/client-operation-0')
        self.assertEqual(denied.status_code, 404)

    def test_busy_account_stays_in_fifo_queue_and_retries_do_not_duplicate(self):
        self.assertEqual(self.enqueue(1, 'same').status_code, 202)
        self.assertEqual(self.enqueue(1, 'same').status_code, 202)
        conflict = self.enqueue(1, 'another')
        self.assertEqual(int(conflict.headers.get('X-Eve-Status', conflict.status_code)), 409)
        self.assertEqual(self.enqueue(2, 'SAME', actor=1).status_code, 202)
        self.assertEqual(self.enqueue(3, 'different', actor=1).status_code, 202)
        with app.app_context():
            first = queue._claim_job(set(), set())
            with patch('panel.routes.clients._acquire_renew_lock', return_value=None):
                queue._execute(app, first[0])
            self.assertEqual(db.session.get(queue.Job, first[0]).state, 'queued')
            self.assertEqual(queue.Job.query.count(), 3)
            self.assertIsNotNone(queue._claim_job(set(), set()))
            self.assertIsNone(queue._claim_job(set(), set()))
            db.session.get(queue.Job, first[0]).ready_at = queue._now() - timedelta(seconds=1)
            db.session.commit()
            self.assertEqual(queue._claim_job(set(), set())[0], first[0])

    def test_edit_membership_and_fields_run_in_order_for_the_same_job(self):
        response = self.enqueue(4, 'old', action='edit', payload={
            'new_email': 'new', 'membership': {'mode': 'set', 'inbound_ids': [9]},
        })
        self.assertEqual(response.status_code, 202)
        calls = []

        def membership(server_id, email):
            calls.append(('membership', email, request.json['inbound_ids']))
            return jsonify(success=True)

        def edit(server_id, inbound_id, email):
            calls.append(('edit', email, inbound_id))
            return jsonify(success=True)

        with app.app_context(), patch.dict(app.view_functions, {
            'clients.set_client_inbounds': membership, 'clients.edit_client': edit,
        }), patch('panel.routes.clients._acquire_renew_lock', return_value='lock'):
            job_id, _, _ = queue._claim_job(set(), set())
            queue._execute(app, job_id)
        self.assertEqual(calls, [('membership', 'old', [9]), ('edit', 'old', 9)])
        result = self.clients[0].get('/api/client-jobs/client-operation-4').get_json()
        self.assertEqual(result['result']['body'], {'success': True, 'inbound_id': 9})

    def test_migration_can_run_after_create_all_or_on_an_existing_database(self):
        path = os.path.join(app.root_path, 'alembic', 'versions', '19d49347f3c2_client_mutation_queue.py')
        migration = run_path(path)
        engine = sa.create_engine('sqlite://')
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql('CREATE TABLE admins (id INTEGER PRIMARY KEY)')
                with Operations.context(MigrationContext.configure(connection)):
                    migration['upgrade']()
                    migration['upgrade']()
                indexes = sa.inspect(connection).get_indexes(queue.Job.__tablename__)
                self.assertEqual({item['name'] for item in indexes},
                                 {'ix_client_mutation_jobs_ready', 'ix_client_mutation_jobs_server'})
        finally:
            engine.dispose()

    def test_job_does_not_run_if_its_panel_address_changes(self):
        self.assertEqual(self.enqueue(9, 'account').status_code, 202)
        with app.app_context():
            panel = db.session.get(Server, 7)
            original_host = panel.host
            panel.host = 'https://replacement.invalid'
            db.session.commit()
            try:
                job_id, _, _ = queue._claim_job(set(), set())
                with patch.dict(app.view_functions, {'clients.renew_client': self.fail}):
                    queue._execute(app, job_id)
            finally:
                panel.host = original_host
                db.session.commit()
        job = self.clients[0].get('/api/client-jobs/client-operation-9').get_json()
        self.assertEqual(job['result']['status'], 409)
