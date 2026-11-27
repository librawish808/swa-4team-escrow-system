"""Integration check against the configured PostgreSQL DB (creates test rows only).

Run from backend: .venv/bin/python -m unittest discover -s tests -v
Set QAS02_ROUNDS=100 for 100 independent full service races.
"""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.database import connect_db
from app.transaction_service import TransactionConflict, claim_refund, claim_waybill
from app.transaction_service import register_waybill, cancel_transaction
from app.external_ports import ExternalResult, MockExternalPort


class ClaimIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.ids = []

    def tearDown(self):
        if self.ids:
            with connect_db() as db:
                db.execute("DELETE FROM public.transactions WHERE id = ANY(%s)", (self.ids,))

    def new_transaction(self):
        with connect_db() as db:
            row = db.execute(
                """INSERT INTO public.transactions (buyer_id, seller_id, amount)
                   VALUES (20, 10, 100000) RETURNING id"""
            ).fetchone()
        self.ids.append(row["id"])
        return row["id"]

    def test_two_requests_have_exactly_one_winner(self):
        for iteration in range(int(os.getenv("QAS02_ROUNDS", "1"))):
            with self.subTest(iteration=iteration):
                transaction_id = self.new_transaction()
                barrier = Barrier(2)

                def request(claim, actor):
                    barrier.wait(timeout=10)
                    try:
                        return claim(transaction_id, 1, actor)
                    except TransactionConflict:
                        return None

                with ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(request, claim_waybill, 10)
                    b = pool.submit(request, claim_refund, 20)
                    results = [a.result(timeout=30), b.result(timeout=30)]
                winners = [row for row in results if row is not None]
                self.assertEqual(len(winners), 1)
                self.assertEqual(results.count(None), 1)
                with connect_db() as db:
                    final = db.execute(
                        "SELECT id, status, version, amount FROM public.transactions WHERE id = %s",
                        (transaction_id,),
                    ).fetchone()
                self.assertEqual(final, winners[0])
                self.assertEqual(final["version"], 2)

    def test_wrong_actor_and_stale_version_cannot_claim(self):
        transaction_id = self.new_transaction()
        for claim, actor, version in (
            (claim_waybill, 20, 1),
            (claim_refund, 10, 1),
            (claim_waybill, 10, 2),
        ):
            with self.assertRaises(TransactionConflict):
                claim(transaction_id, version, actor)
        winner = claim_waybill(transaction_id, 1, 10)
        self.assertEqual(winner["status"], "WAYBILL_PROCESSING")
        with self.assertRaises(TransactionConflict):
            claim_refund(transaction_id, 2, 20)

    def test_external_calls_and_final_state_match_for_100_races(self):
        def race(iteration):
            transaction_id = self.new_transaction()
            port = MockExternalPort()
            barrier = Barrier(2)

            def request(waybill):
                barrier.wait(timeout=10)
                try:
                    if waybill:
                        return register_waybill(transaction_id, 1, 10, "TEST123", port)
                    return cancel_transaction(transaction_id, 1, 20, port)
                except TransactionConflict:
                    return None

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(request, flag) for flag in (True, False)]
                results = [future.result(timeout=30) for future in futures]
            self.assertEqual(results.count(None), 1)
            self.assertEqual(len(port.calls), 1)
            winner = next(row for row in results if row is not None)
            expected_status = ("WAYBILL_REGISTERED" if port.calls[0]["operation"] == "WAYBILL"
                               else "CANCELLED")
            with connect_db() as db:
                final = db.execute(
                    "SELECT status, version, waybill_no FROM public.transactions WHERE id = %s",
                    (transaction_id,),
                ).fetchone()
            self.assertEqual(final["status"], expected_status)
            self.assertEqual(winner["status"], expected_status)
            self.assertEqual(final["version"], 3)
            self.assertEqual(port.calls[0]["amount"], 100000)
            self.assertEqual(final["waybill_no"], "TEST123" if expected_status == "WAYBILL_REGISTERED" else None)
            with self.assertRaises(TransactionConflict):
                cancel_transaction(transaction_id, 1, 20, port)
            self.assertEqual(len(port.calls), 1)
            if (iteration + 1) % 10 == 0:
                print(f"Completed race: {iteration + 1}", flush=True)

        with ThreadPoolExecutor(max_workers=4) as rounds:
            list(rounds.map(race, range(int(os.getenv("QAS02_ROUNDS", "100")))))

    def test_failed_unknown_and_timeout_keep_claim_and_block_replay(self):
        class TimeoutPort:
            def execute(self, **request):
                raise TimeoutError("simulated ambiguous response")

        for operation in ("WAYBILL", "REFUND"):
            for outcome in (ExternalResult.FAILED, ExternalResult.UNKNOWN, "TIMEOUT"):
                transaction_id = self.new_transaction()
                port = TimeoutPort() if outcome == "TIMEOUT" else MockExternalPort(outcome)
                if operation == "WAYBILL":
                    result = register_waybill(transaction_id, 1, 10, "TEST123", port)
                else:
                    result = cancel_transaction(transaction_id, 1, 20, port)
                self.assertTrue(result["recovery_required"])
                self.assertEqual(result["status"], f"{operation}_PROCESSING")
                self.assertEqual(result["external_result"], "UNKNOWN" if outcome == "TIMEOUT" else outcome.value)
                with connect_db() as db:
                    final = db.execute("SELECT status, version FROM public.transactions WHERE id = %s",
                                       (transaction_id,)).fetchone()
                self.assertEqual(final, {"status": f"{operation}_PROCESSING", "version": 2})
                for claim, actor in ((claim_waybill, 10), (claim_refund, 20)):
                    with self.assertRaises(TransactionConflict):
                        claim(transaction_id, 2, actor)

    def test_each_operation_can_complete_successfully(self):
        for operation in ("WAYBILL", "REFUND"):
            transaction_id = self.new_transaction()
            port = MockExternalPort()
            result = (register_waybill(transaction_id, 1, 10, "TEST123", port)
                      if operation == "WAYBILL" else cancel_transaction(transaction_id, 1, 20, port))
            self.assertEqual(result["status"], "WAYBILL_REGISTERED" if operation == "WAYBILL" else "CANCELLED")
            self.assertEqual(result["version"], 3)
            self.assertFalse(result["recovery_required"])
            self.assertEqual(len(port.calls), 1)
            # The exact same key also represents one operation at the mock provider.
            port.execute(**port.calls[0])
            self.assertEqual(len(port.calls), 1)
