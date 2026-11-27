"""QAS-03 데모 API 테스트. FastAPI가 설치된 환경에서만 실행된다.

실행 (backend 폴더에서):  python -m unittest tests.test_qas03_api -v
"""
from __future__ import annotations

import logging
import os
import tempfile
import time
import unittest

try:
    from fastapi.testclient import TestClient
except ImportError:  # FastAPI 없이 단위 테스트만 돌리는 경우
    TestClient = None

logging.disable(logging.CRITICAL)


@unittest.skipIf(TestClient is None, "fastapi/httpx가 설치되어 있지 않음")
class Qas03ApiTest(unittest.TestCase):
    def setUp(self):
        from app import qas03_api
        from app.qas03_demo import DemoRuntime

        fd, self.db = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        qas03_api._runtime = DemoRuntime(self.db, timeout_millis=100, job_interval_seconds=0.2)
        self.rt = qas03_api._runtime
        from app.main import app

        self.client = TestClient(app)

    def tearDown(self):
        from app import qas03_api

        self.rt.system.shutdown()
        qas03_api._runtime = None
        os.remove(self.db)

    def state(self):
        return self.client.get("/api/qas-03/state").json()

    def tx(self, tx_id):
        return next(t for t in self.state()["transactions"] if t["id"] == tx_id)

    def wait_status(self, tx_id, status, seconds=5):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.tx(tx_id)["status"] == status:
                return True
            time.sleep(0.05)
        return False

    def test_bank_outage_then_automatic_recovery(self):
        tx_id = self.client.post("/api/qas-03/transactions", json={}).json()["id"]
        self.client.post("/api/qas-03/bank-fault", json={"fault": "RESPONSE_LOST"})

        r = self.client.post(f"/api/qas-03/transactions/{tx_id}/confirm-purchase").json()
        self.assertEqual((r["code"], r["status"]), ("PROCESSING", "PAYOUT_PROCESSING"))
        again = self.client.post(f"/api/qas-03/transactions/{tx_id}/confirm-purchase").json()
        self.assertEqual(again["code"], "DUPLICATE_REQUEST")

        self.client.post("/api/qas-03/bank-fault", json={"fault": "NONE"})
        self.assertTrue(self.wait_status(tx_id, "COMPLETED"))  # 백그라운드 복구 작업이 자동 확정
        self.assertEqual(self.tx(tx_id)["bank_executed"], 1)

    def test_crash_restart_recovers_on_startup(self):
        tx_id = self.client.post("/api/qas-03/transactions", json={}).json()["id"]
        r = self.client.post(f"/api/qas-03/transactions/{tx_id}/crash-restart", json={"after_send": False})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.wait_status(tx_id, "COMPLETED"))
        s = self.state()
        self.assertEqual(s["job"]["restarts"], 1)
        transfer = next(t for t in s["transfers"] if t["transaction_id"] == tx_id)
        self.assertEqual(transfer["attempts"], 1)

    def test_refund_flow_and_unknown_transaction(self):
        tx_id = self.client.post("/api/qas-03/transactions", json={"stage": "DEPOSIT_CONFIRMED"}).json()["id"]
        self.client.post("/api/qas-03/bank-fault", json={"fault": "DOWN"})
        r = self.client.post(f"/api/qas-03/transactions/{tx_id}/cancel").json()
        self.assertEqual(r["status"], "REFUND_PROCESSING")
        self.client.post("/api/qas-03/bank-fault", json={"fault": "NONE"})
        self.assertTrue(self.wait_status(tx_id, "CANCELLED"))
        self.assertEqual(self.client.post("/api/qas-03/transactions/nope/cancel").status_code, 404)

    def test_benchmark_endpoint_passes(self):
        result = self.client.post("/api/qas-03/benchmark").json()
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["duplicate_payouts"], 0)


if __name__ == "__main__":
    unittest.main()
