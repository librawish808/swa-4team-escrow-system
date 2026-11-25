import asyncio
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import perf_counter

from app.adapters import MockBankPort, MockDeliveryPort
from app.benchmark import run_qas01
from app.domain import Decision, DomainError, ShipmentResult, instant, utc_now
from app.repository import SqlTransactionRepository
from app.runner import DeadlineRecoveryRunner
from app.service import TransactionService


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "test.sqlite3")
        self.repo = SqlTransactionRepository(self.path)
        self.deadline = datetime(2026, 11, 26, 9, tzinfo=UTC)
        self.now = self.deadline - timedelta(milliseconds=50)
        self.service = TransactionService(
            self.repo,
            MockDeliveryPort(0),
            MockBankPort(self.repo),
            clock=lambda: self.now,
        )
        self.id = self.repo.create_approved_return(self.deadline - timedelta(hours=72))

    def tearDown(self):
        self.temp.cleanup()

    def accept(self, number="VALID-001", request="r1", actor="buyer-demo"):
        return self.service.register_return_waybill(self.id, actor, request, number)

    async def verify_next(self):
        receipt = self.repo.claim_receipt(self.now)
        self.assertIsNotNone(receipt)
        await self.service.verify_receipt(receipt)

    async def test_stale_deadline_read_must_recheck_receipt_without_waiting_for_carrier(
        self,
    ):
        stale = self.repo.get(self.id)
        self.accept()
        self.service.delivery = MockDeliveryPort(3)
        task = asyncio.create_task(
            self.service.verify_receipt(self.repo.claim_receipt(self.now))
        )
        await asyncio.sleep(0.05)
        self.now = self.deadline
        started = perf_counter()
        self.assertEqual(self.service.evaluate(self.id, stale), Decision.HOLD)
        self.assertLess((perf_counter() - started) * 1000, 200)
        self.assertFalse(task.done())
        self.assertEqual(
            self.service.get_transaction_status(self.id)["return_label"],
            "운송장 확인 중",
        )
        self.assertEqual(self.repo.get(self.id)["operations"], [])
        await task
        self.assertEqual(
            self.service.get_transaction_status(self.id)["return_label"], "반품 진행"
        )
        self.assertEqual(self.repo.get(self.id)["operations"], [])
        self.service.mark_mock_arrived(self.id)
        await self.service.dispatch_transfers()
        self.assertEqual(self.repo.transfer_count("PAYOUT"), 0)
        self.assertEqual(self.repo.transfer_count("REFUND"), 1)

    async def test_one_hundred_cases_with_real_three_second_carrier_delay(self):
        report = await run_qas01()
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["hold_decisions"], 100)
        self.assertEqual(report["payout_instructions"], 0)
        self.assertEqual(report["payout_transfers"], 0)
        self.assertLessEqual(report["max_decision_ms"], 200)
        print("QAS-01 benchmark:", report)

    def test_cutoff_minus_one_ms_is_accepted_exact_and_plus_one_ms_are_rejected(self):
        self.now = self.deadline - timedelta(milliseconds=1)
        self.accept()
        for offset in (0, 1):
            self.now = self.deadline + timedelta(milliseconds=offset)
            with self.subTest(offset=offset), self.assertRaises(DomainError):
                self.accept(request=f"late-{offset}")
        self.assertEqual(len(self.repo.get(self.id)["receipts"]), 1)

    async def test_missing_waybill_pays_only_at_deadline_and_once(self):
        self.assertEqual(self.service.evaluate(self.id), Decision.WAIT)
        self.now = self.deadline
        self.assertEqual(self.service.evaluate(self.id), Decision.PAYOUT)
        for _ in range(3):
            self.service.evaluate(self.id)
            await self.service.dispatch_transfers()
        self.assertEqual(len(self.repo.get(self.id)["operations"]), 1)
        self.assertEqual(self.repo.transfer_count("PAYOUT"), 1)

    async def test_invalid_and_wrong_recipient_release_only_after_deadline(self):
        for number in ("INVALID-001", "WRONG-001"):
            with self.subTest(number=number):
                self.id = self.repo.create_approved_return(
                    self.deadline - timedelta(hours=72)
                )
                self.now = self.deadline - timedelta(milliseconds=1)
                self.accept(number)
                await self.verify_next()
                self.assertEqual(
                    self.repo.get(self.id)["receipts"][0]["status"], "INVALID"
                )
                self.assertEqual(self.repo.get(self.id)["operations"], [])
                self.now = self.deadline
                self.assertEqual(self.service.evaluate(self.id), Decision.PAYOUT)

    async def test_invalid_waybill_does_not_ignore_another_pending_or_valid_receipt(
        self,
    ):
        self.accept("INVALID-001", "bad")
        self.accept("VALID-001", "good")
        self.now = self.deadline
        await self.verify_next()
        self.assertEqual(self.service.evaluate(self.id), Decision.HOLD)
        await self.verify_next()
        self.assertEqual(self.service.evaluate(self.id), Decision.HOLD)
        self.assertEqual(self.repo.get(self.id)["operations"], [])

    async def test_carrier_unavailable_retries_then_escalates_without_releasing_funds(
        self,
    ):
        self.accept("TIMEOUT-001")
        self.now = self.deadline
        for attempt in range(3):
            await self.verify_next()
            self.now += timedelta(seconds=1)
        value = self.service.get_transaction_status(self.id)
        self.assertEqual(value["receipts"][0]["attempts"], 3)
        self.assertEqual(value["receipts"][0]["status"], "MANUAL_REVIEW")
        self.assertTrue(value["payment_held"])
        self.assertEqual(self.service.evaluate(self.id), Decision.HOLD)
        self.assertIsNone(self.repo.claim_receipt(self.now))
        self.assertEqual(value["operations"], [])

    async def test_effective_valid_return_overrides_another_receipts_manual_review(
        self,
    ):
        self.accept("TIMEOUT-001", "unknown")
        self.accept("VALID-001", "valid")
        unknown = self.repo.claim_receipt(self.now)
        self.repo.finish_receipt(unknown, ShipmentResult(None), self.now, 1, 1)
        await self.verify_next()
        self.assertEqual(
            self.service.get_transaction_status(self.id)["return_label"], "반품 진행"
        )
        self.service.mark_mock_arrived(self.id)
        await self.service.dispatch_transfers()
        self.assertEqual(
            self.service.get_transaction_status(self.id)["return_label"], "환불 완료"
        )
        self.assertEqual(self.repo.transfer_count("PAYOUT"), 0)

    def test_waiting_for_db_is_not_an_acknowledged_receipt_before_cutoff(self):
        entered = Event()

        def waiting_request():
            entered.set()
            return self.accept()

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.repo.connection(write=True):
                future = pool.submit(waiting_request)
                self.assertTrue(entered.wait(1))
                self.now = self.deadline
            with self.assertRaises(DomainError):
                future.result(timeout=2)
        self.assertEqual(self.repo.get(self.id)["receipts"], [])
        self.assertEqual(self.service.evaluate(self.id), Decision.PAYOUT)

    def test_request_replay_keeps_original_timestamp_and_version_after_cutoff(self):
        receipt_id = self.accept()
        initial = self.repo.get(self.id)
        self.now = self.deadline + timedelta(seconds=10)
        self.assertEqual(self.accept(), receipt_id)
        replay = self.repo.get(self.id)
        self.assertEqual(replay["version"], initial["version"])
        self.assertEqual(replay["receipts"], initial["receipts"])
        with self.assertRaises(DomainError):
            self.accept("VALID-002")

    def test_receipt_and_read_require_transaction_participant(self):
        with self.assertRaises(DomainError) as error:
            self.accept(actor="seller-demo")
        self.assertEqual(error.exception.status_code, 403)
        with self.assertRaises(DomainError):
            self.service.get_transaction_status(self.id, "someone-else")
        buyer = self.service.get_transaction_status(self.id, "buyer-demo")
        seller = self.service.get_transaction_status(self.id, "seller-demo")
        for key in ("status", "return_label", "payment_held", "version"):
            self.assertEqual(buyer[key], seller[key])

    async def test_refund_requires_arrival_and_repeated_arrival_is_idempotent(self):
        self.accept()
        with self.assertRaises(DomainError):
            self.service.mark_mock_arrived(self.id)
        await self.verify_next()
        self.assertEqual(self.repo.get(self.id)["operations"], [])
        for _ in range(2):
            self.service.mark_mock_arrived(self.id)
            await self.service.dispatch_transfers()
        self.assertEqual(self.repo.transfer_count("REFUND"), 1)
        self.assertEqual(self.repo.transfer_count("PAYOUT"), 0)
        self.assertEqual(
            self.service.get_transaction_status(self.id)["return_label"], "환불 완료"
        )

    async def test_persisted_receipt_survives_new_repository_and_runner(self):
        self.accept()
        fresh = SqlTransactionRepository(self.path)
        self.now = self.deadline
        restored = TransactionService(
            fresh, MockDeliveryPort(0), MockBankPort(fresh), clock=lambda: self.now
        )
        runner = DeadlineRecoveryRunner(restored)
        await runner.start()
        try:
            for _ in range(40):
                if (
                    restored.get_transaction_status(self.id)["return_label"]
                    == "반품 진행"
                ):
                    break
                await asyncio.sleep(0.025)
            else:
                self.fail("Persisted receipt was not resumed")
            self.assertEqual(fresh.transfer_count("PAYOUT"), 0)
        finally:
            await runner.stop()

    async def test_live_runner_decides_hold_within_200ms_while_carrier_is_waiting(self):
        deadline = utc_now() + timedelta(milliseconds=300)
        transaction_id = self.repo.create_approved_return(
            deadline - timedelta(hours=72)
        )
        service = TransactionService(
            self.repo, MockDeliveryPort(3), MockBankPort(self.repo)
        )
        runner = DeadlineRecoveryRunner(service)
        await runner.start()
        try:
            await asyncio.sleep(max(0, (deadline - utc_now()).total_seconds() - 0.05))
            service.register_return_waybill(
                transaction_id, "buyer-demo", "live", "VALID-001"
            )
            for _ in range(80):
                value = self.repo.get(transaction_id)
                if value["hold_decided_at"] is not None:
                    break
                await asyncio.sleep(0.005)
            else:
                self.fail("Live deadline runner did not decide the hold")
            lag_ms = (
                instant(value["hold_decided_at"]) - deadline
            ).total_seconds() * 1000
            self.assertGreaterEqual(lag_ms, 0)
            self.assertLessEqual(lag_ms, 200)
            self.assertLessEqual((utc_now() - deadline).total_seconds() * 1000, 200)
            self.assertEqual(value["receipts"][0]["status"], "PENDING")
            self.assertEqual(value["operations"], [])
        finally:
            await runner.stop()

    def test_expired_worker_lease_cannot_overwrite_new_owner_result(self):
        self.accept()
        old = self.repo.claim_receipt(self.now, lease_seconds=1)
        self.assertIsNone(self.repo.claim_receipt(self.now))
        self.now += timedelta(seconds=2)
        new = self.repo.claim_receipt(self.now)
        self.assertTrue(
            self.repo.finish_receipt(
                new, ShipmentResult(True, "seller-demo"), self.now, 3, 1
            )
        )
        self.assertFalse(
            self.repo.finish_receipt(old, ShipmentResult(False), self.now, 3, 1)
        )
        self.assertEqual(self.repo.get(self.id)["receipts"][0]["status"], "VALID")

    def test_parallel_expiry_workers_create_only_one_principal_instruction(self):
        self.now = self.deadline
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.service.evaluate(self.id), range(20)))
        self.assertEqual(len(self.repo.get(self.id)["operations"]), 1)

    async def test_bank_effect_before_result_save_is_replayed_with_same_key(self):
        self.now = self.deadline
        self.service.evaluate(self.id)
        operation = self.repo.pending_operations()[0]
        self.repo.record_mock_transfer(
            operation
        )  # bank effect, application result not saved
        restarted = TransactionService(
            SqlTransactionRepository(self.path),
            MockDeliveryPort(0),
            MockBankPort(self.repo),
            clock=lambda: self.now,
        )
        await restarted.dispatch_transfers()
        self.assertEqual(self.repo.transfer_count("PAYOUT"), 1)
        self.assertEqual(self.repo.get(self.id)["operations"][0]["status"], "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
