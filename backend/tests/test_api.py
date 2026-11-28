import unittest
from unittest.mock import patch

import psycopg
from fastapi.testclient import TestClient

from app.main import create_app, current_actor, external_port
from app.external_ports import MockExternalPort
from app.transaction_service import TransactionConflict


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.dependency_overrides[current_actor] = lambda: 10
        self.app.dependency_overrides[external_port] = MockExternalPort
        self.client = TestClient(self.app)

    def test_default_app_requires_real_auth(self):
        response = TestClient(create_app()).post('/transactions/1/cancel', json={'version': 1})
        self.assertEqual(response.status_code, 503)

    def test_invalid_body_is_rejected_before_service(self):
        with patch('app.main.service.register_waybill') as service:
            for body in ({'version': 0, 'waybill_no': 'A'},
                         {'version': 1, 'waybill_no': '   '},
                         {'version': 1, 'waybill_no': 'A', 'seller_id': 10}):
                response = self.client.post('/transactions/1/waybill', json=body)
                self.assertEqual(response.status_code, 422)
            service.assert_not_called()

    def test_conflict_is_http_409(self):
        with patch('app.main.service.cancel_transaction', side_effect=TransactionConflict):
            self.assertEqual(self.client.post('/transactions/1/cancel', json={'version': 1}).status_code, 409)

    def test_database_error_hides_details(self):
        with patch('app.main.service.cancel_transaction', side_effect=psycopg.OperationalError('SECRET')):
            response = self.client.post('/transactions/1/cancel', json={'version': 1})
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('SECRET', response.text)

    def test_pending_result_is_http_202(self):
        with patch('app.main.service.cancel_transaction', return_value={'recovery_required': True}):
            self.assertEqual(self.client.post('/transactions/1/cancel', json={'version': 1}).status_code, 202)

    def test_demo_rejects_remote_clients(self):
        from app.demo import app
        client = TestClient(app, client=('203.0.113.1', 12345))
        self.assertEqual(client.post('/demo/transactions').status_code, 403)

    def test_demo_docs_include_required_actor_header(self):
        from app.demo import app
        client = TestClient(app)
        paths = client.get('/openapi.json').json()['paths']
        for action in ('waybill', 'cancel'):
            parameters = paths[f'/transactions/{{transaction_id}}/{action}']['post']['parameters']
            header = next(p for p in parameters if p['name'] == 'X-Demo-Actor-Id')
            self.assertEqual(header['in'], 'header')
            self.assertTrue(header['required'])
            self.assertEqual(header['schema']['enum'], ['10', '20'])
        self.assertEqual(client.post('/transactions/1/cancel', json={'version': 1}).status_code, 422)


class ApiDatabaseTests(unittest.TestCase):
    """Real DB, separate disposable demo rows; no existing transaction is modified."""

    def test_both_success_paths_and_replay_conflict(self):
        from app.database import connect_db
        from app.demo import app
        client = TestClient(app)
        ids = []
        try:
            for action, actor, final in (('waybill', '10', 'WAYBILL_REGISTERED'),
                                         ('cancel', '20', 'CANCELLED')):
                created = client.post('/demo/transactions')
                self.assertEqual(created.status_code, 201)
                transaction_id = created.json()['id']
                ids.append(transaction_id)
                body = {'version': 1}
                if action == 'waybill':
                    body['waybill_no'] = 'API-TEST123'
                path = f'/transactions/{transaction_id}/{action}'
                response = client.post(path, json=body, headers={'X-Demo-Actor-Id': actor})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['status'], final)
                self.assertEqual(response.json()['version'], 3)
                self.assertEqual(client.post(path, json=body, headers={'X-Demo-Actor-Id': actor}).status_code, 409)
        finally:
            if ids:
                with connect_db() as db:
                    db.execute('DELETE FROM public.transactions WHERE id = ANY(%s)', (ids,))
