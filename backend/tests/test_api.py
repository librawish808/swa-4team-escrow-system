import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app


class ApiTests(unittest.TestCase):
    def test_benchmark_setup_does_not_block_live_api_event_loop(self):
        entered, release = Event(), Event()

        async def benchmark_with_blocking_setup():
            entered.set()
            release.wait(timeout=1)
            return {"passed": True}

        with (
            TemporaryDirectory() as directory,
            TestClient(create_app(str(Path(directory) / "api.sqlite3"))) as client,
            patch("app.main.run_qas01", benchmark_with_blocking_setup),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            result = pool.submit(client.post, "/api/demo/qas-01")
            self.assertTrue(entered.wait(timeout=1))
            try:
                started = time.perf_counter()
                self.assertEqual(client.get("/").status_code, 200)
                self.assertLess(time.perf_counter() - started, 0.2)
            finally:
                release.set()
            self.assertEqual(result.result(timeout=2).json(), {"passed": True})

    def test_receipt_returns_immediately_then_both_participants_see_return_and_refund(
        self,
    ):
        with TemporaryDirectory() as directory:
            app = create_app(
                str(Path(directory) / "api.sqlite3"), carrier_delay_seconds=0.2
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/demo/transactions", json={"deadline_in_seconds": 15}
                )
                self.assertEqual(created.status_code, 201)
                transaction_id = created.json()["id"]
                path = f"/api/transactions/{transaction_id}"
                submitted = client.post(
                    path + "/return-waybills",
                    json={
                        "actor_id": "buyer-demo",
                        "request_id": "request-1",
                        "waybill_no": "VALID-001",
                    },
                )
                self.assertEqual(submitted.status_code, 202)
                self.assertEqual(submitted.json()["return_label"], "운송장 확인 중")
                self.assertEqual(
                    client.get(path, params={"actor_id": "outsider"}).status_code, 403
                )
                for _ in range(40):
                    buyer = client.get(path, params={"actor_id": "buyer-demo"}).json()
                    if buyer["return_label"] == "반품 진행":
                        break
                    time.sleep(0.025)
                else:
                    self.fail("Carrier worker did not register return")
                seller = client.get(path, params={"actor_id": "seller-demo"}).json()
                self.assertEqual(buyer["return_label"], seller["return_label"])
                self.assertEqual(buyer["operations"], [])
                self.assertEqual(
                    client.post(
                        path.replace("/api/", "/api/demo/") + "/arrive"
                    ).status_code,
                    200,
                )
                for _ in range(40):
                    status = client.get(path, params={"actor_id": "buyer-demo"}).json()
                    if status["return_label"] == "환불 완료":
                        break
                    time.sleep(0.025)
                else:
                    self.fail("Mock bank did not complete refund")
                self.assertEqual(status["operations"][0]["kind"], "REFUND")
                self.assertEqual(client.get("/api/transactions").status_code, 200)

    def test_invalid_inputs_missing_transaction_and_unauthorized_submission(self):
        with (
            TemporaryDirectory() as directory,
            TestClient(create_app(str(Path(directory) / "api.sqlite3"))) as client,
        ):
            self.assertEqual(
                client.post(
                    "/api/demo/transactions", json={"deadline_in_seconds": 0}
                ).status_code,
                422,
            )
            self.assertEqual(
                client.get("/api/transactions/missing?actor_id=buyer-demo").status_code,
                404,
            )
            value = client.post("/api/demo/transactions", json={}).json()
            endpoint = f"/api/transactions/{value['id']}/return-waybills"
            body = {
                "actor_id": "seller-demo",
                "request_id": "r",
                "waybill_no": "VALID-001",
            }
            self.assertEqual(client.post(endpoint, json=body).status_code, 403)
            body["actor_id"] = "buyer-demo"
            body["received_at"] = (
                "1900-01-01T00:00:00Z"  # ignored, cannot override server clock
            )
            self.assertEqual(client.post(endpoint, json=body).status_code, 202)
            body["waybill_no"] = "VALID-002"
            self.assertEqual(client.post(endpoint, json=body).status_code, 409)
