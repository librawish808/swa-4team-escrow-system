"""Chaos fault-injection, relational invariant, bulkhead, and 1,000-case stress tests."""

import asyncio
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from app.adapters import MockBankPort, MockDeliveryPort
from app.benchmark import percentile, run_qas01_stress
from app.domain import (
    Deposit,
    OperationStatus,
    PolicyContext,
    ShipmentResult,
    TransactionStatus,
    TransferResult,
    TransferStatus,
)
from app.invariants import InvariantChecker, verify_money_conservation
from app.policy import DepositPolicy
from app.repository import SqlTransactionRepository
from app.runner import DeadlineRecoveryRunner
from app.service import TransactionService


class ChaosAndInvariantTests(unittest.IsolatedAsyncioTestCase):
    async def test_stress_1000_cases_tail_latency_and_zero_invariant_violations(self):
        report = await run_qas01_stress(runs=1000, carrier_delay_seconds=3.0)
        print("QAS-01 1000-case stress:", report)
        self.assertTrue(report["passed"], report)
        self.assertEqual(report["hold_decisions"], 1000)
        self.assertEqual(report["payout_instructions"], 0)
        self.assertEqual(report["payout_transfers"], 0)
        self.assertLessEqual(report["p95_ms"], 50)
        self.assertLessEqual(report["p99_ms"], 100)
        self.assertLessEqual(report["max_case_ms"], 200)
        self.assertTrue(report["invariants_passed"])
        self.assertEqual(report["invariant_violations"], [])

    async def test_crash_recovery_all_four_fault_injection_points(self):
        with TemporaryDirectory() as directory:
            path = str(Path(directory) / "chaos.sqlite3")
            deadline = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
            now = deadline - timedelta(milliseconds=50)

            # Crash 1: Process dies immediately after waybill persisted, before carrier lookup
            repo1 = SqlTransactionRepository(path)
            svc1 = TransactionService(
                repo1, MockDeliveryPort(0), MockBankPort(repo1), clock=lambda: now
            )
            tx1 = repo1.create_approved_return(deadline - timedelta(hours=72))
            svc1.register_return_waybill(tx1, "buyer-demo", "c1", "VALID-001")

            # Restart with fresh repository & runner (RTO <= 5s)
            now = deadline
            restart_start = perf_counter()
            repo1_restarted = SqlTransactionRepository(path)
            svc1_restarted = TransactionService(
                repo1_restarted,
                MockDeliveryPort(0),
                MockBankPort(repo1_restarted),
                clock=lambda: now,
            )
            runner = DeadlineRecoveryRunner(svc1_restarted)
            await runner.start()
            try:
                for _ in range(100):
                    if (
                        svc1_restarted.get_transaction_status(tx1)["return_label"]
                        == "반품 진행"
                    ):
                        break
                    await asyncio.sleep(0.05)
                else:
                    self.fail("Crash 1 recovery timed out")
                self.assertLessEqual(perf_counter() - restart_start, 5.0)
            finally:
                await runner.stop()

            # Crash 2: Worker crashes while holding carrier lease; new worker takes over; zombie blocked
            now = deadline - timedelta(milliseconds=50)
            tx2 = repo1_restarted.create_approved_return(deadline - timedelta(hours=72))
            svc1_restarted.register_return_waybill(tx2, "buyer-demo", "c2", "VALID-002")
            zombie_claim = repo1_restarted.claim_receipt(now, lease_seconds=1)
            self.assertIsNotNone(zombie_claim)
            now = now + timedelta(seconds=2)
            new_claim = repo1_restarted.claim_receipt(now, lease_seconds=30)
            self.assertIsNotNone(new_claim)
            self.assertTrue(
                repo1_restarted.finish_receipt(
                    new_claim, ShipmentResult(True, "seller-demo"), now, 3, 1
                )
            )
            # Zombie wakes up late with INVALID -> Fencing Token rejects overwrite
            self.assertFalse(
                repo1_restarted.finish_receipt(
                    zombie_claim, ShipmentResult(False), now, 3, 1
                )
            )
            self.assertEqual(
                svc1_restarted.get_transaction_status(tx2)["return_label"], "반품 진행"
            )

            # Crash 3: Process dies right after PAYOUT operation persisted, before bank call
            now = deadline
            tx3 = repo1_restarted.create_approved_return(deadline - timedelta(hours=72))
            svc1_restarted.evaluate(tx3)
            self.assertEqual(
                repo1_restarted.get(tx3)["operations"][0]["status"],
                OperationStatus.PENDING,
            )
            repo3_restarted = SqlTransactionRepository(path)
            svc3_restarted = TransactionService(
                repo3_restarted,
                MockDeliveryPort(0),
                MockBankPort(repo3_restarted),
                clock=lambda: now,
            )
            await svc3_restarted.dispatch_transfers()
            self.assertEqual(
                repo3_restarted.get(tx3)["operations"][0]["status"],
                OperationStatus.SUCCEEDED,
            )

            # Crash 4: Bank succeeded, but response lost (UNKNOWN) before DB saved
            tx4 = repo3_restarted.create_approved_return(deadline - timedelta(hours=72))
            svc3_restarted.evaluate(tx4)
            op4 = repo3_restarted.get(tx4)["operations"][0]
            repo3_restarted.record_mock_transfer(op4)  # bank effect already happened
            repo3_restarted.save_transfer_result(
                op4["request_key"], OperationStatus.UNKNOWN, now
            )
            repo4_restarted = SqlTransactionRepository(path)
            svc4_restarted = TransactionService(
                repo4_restarted,
                MockDeliveryPort(0),
                MockBankPort(repo4_restarted),
                clock=lambda: now,
            )
            await svc4_restarted.dispatch_transfers()
            self.assertEqual(
                repo4_restarted.get(tx4)["operations"][0]["status"],
                OperationStatus.SUCCEEDED,
            )
            self.assertEqual(repo4_restarted.transfer_count("PAYOUT"), 2)

            # Global DB <-> Bank relational invariant verification
            invariants = InvariantChecker.verify_database(
                repo4_restarted, strict_reconciliation=True
            )
            self.assertTrue(invariants.passed, invariants.violations)
            self.assertEqual(invariants.checked_transactions, 4)

    async def test_total_external_outage_fail_safe_and_bulkhead_latency(self):
        class OutageDeliveryPort:
            async def register_waybill(self, transaction_id, waybill_no):
                raise TimeoutError("carrier down")

            async def get_shipment_status(self, waybill_no, seller_id):
                await asyncio.sleep(0.3)
                raise TimeoutError("carrier timeout")

        class OutageBankPort:
            async def check_deposit(self, transaction_id):
                raise TimeoutError("bank down")

            async def request_payout(self, transaction_id, request_key, operation=None):
                await asyncio.sleep(0.3)
                raise TimeoutError("bank timeout")

            async def request_refund(self, transaction_id, request_key, operation=None):
                await asyncio.sleep(0.3)
                raise TimeoutError("bank timeout")

            async def get_transfer_result(self, request_key):
                await asyncio.sleep(0.3)
                return TransferResult(request_key, TransferStatus.UNKNOWN)

        with TemporaryDirectory() as directory:
            repo = SqlTransactionRepository(str(Path(directory) / "outage.sqlite3"))
            deadline = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
            now = deadline - timedelta(seconds=10)
            service = TransactionService(
                repo, OutageDeliveryPort(), OutageBankPort(), clock=lambda: now
            )

            tx_ids = [
                repo.create_approved_return(deadline - timedelta(hours=72))
                for _ in range(50)
            ]

            # Start background failing carrier verifications
            for tx_id in tx_ids[:10]:
                service.register_return_waybill(tx_id, "buyer-demo", "r0", "VALID-001")
            failing_tasks = [
                asyncio.create_task(service.verify_receipt(repo.claim_receipt(now)))
                for _ in range(10)
            ]
            await asyncio.sleep(0.02)

            # Measure independent waybill acceptance + status reads during 100% external outage
            latencies_ms = []
            for tx_id in tx_ids[10:]:
                started = perf_counter()
                service.register_return_waybill(tx_id, "buyer-demo", "r1", "VALID-001")
                service.get_transaction_status(tx_id, "buyer-demo")
                latencies_ms.append((perf_counter() - started) * 1000)

            await asyncio.gather(*failing_tasks)

            # Advance past deadline: fail-safe must hold ALL 50 (0 payouts!)
            now = deadline
            service.process_due_deadlines()
            self.assertEqual(repo.transfer_count("PAYOUT"), 0)
            for tx_id in tx_ids:
                self.assertEqual(repo.get(tx_id)["operations"], [])
            self.assertLessEqual(percentile(latencies_ms, 99), 50.0)
            self.assertTrue(
                InvariantChecker.verify_database(repo, strict_reconciliation=True).passed
            )

    async def test_ui_db_monotonic_reads_and_symmetric_visibility(self):
        with TemporaryDirectory() as directory:
            repo = SqlTransactionRepository(str(Path(directory) / "ui.sqlite3"))
            deadline = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
            now = deadline - timedelta(seconds=5)
            service = TransactionService(
                repo, MockDeliveryPort(0), MockBankPort(repo), clock=lambda: now
            )
            tx = repo.create_approved_return(deadline - timedelta(hours=72))

            seen_versions = []

            def check_symmetric():
                buyer = service.get_transaction_status(tx, "buyer-demo")
                seller = service.get_transaction_status(tx, "seller-demo")
                for key in ("status", "return_label", "payment_held", "amount", "version"):
                    self.assertEqual(buyer[key], seller[key])
                if seen_versions:
                    self.assertGreaterEqual(buyer["version"], seen_versions[-1])
                seen_versions.append(buyer["version"])
                return buyer["return_label"]

            self.assertEqual(check_symmetric(), "운송장 미접수")
            service.register_return_waybill(tx, "buyer-demo", "u1", "VALID-001")
            self.assertEqual(check_symmetric(), "운송장 확인 중")
            await service.verify_receipt(repo.claim_receipt(now))
            self.assertEqual(check_symmetric(), "반품 진행")
            service.mark_mock_arrived(tx)
            self.assertEqual(check_symmetric(), "반품 도착 · 환불 확인 중")
            await service.dispatch_transfers()
            self.assertEqual(check_symmetric(), "환불 완료")
            self.assertEqual(seen_versions, sorted(seen_versions))
            self.assertEqual(len(set(seen_versions)), len(seen_versions))
            self.assertTrue(InvariantChecker.verify_database(repo).passed)

    def test_invariant_checker_detects_violations_and_verifies_money_conservation(self):
        now = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)
        tx = {
            "id": "tx-m",
            "amount": 100_000,
            "status": TransactionStatus.WAITING_DEPOSIT,
            "version": 1,
        }
        ctx = PolicyContext(now=now, transaction_version=1, transaction=tx)

        # Money conservation holds across normal, excess, and unapplied deposits
        for amount in (100_000, 135_000):
            dep = Deposit(f"dep-{amount}", amount, now)
            decision = DepositPolicy.evaluate_deposit(dep, ctx)
            self.assertEqual(
                verify_money_conservation(dep, decision.metadata["allocations"]), []
            )

        # Negative test: detect conservation violation
        bad_dep = Deposit("dep-bad", 100_000, now)
        good_allocs = DepositPolicy.evaluate_deposit(
            Deposit("dep-ok", 100_000, now), ctx
        ).metadata["allocations"]
        self.assertTrue(
            len(verify_money_conservation(Deposit("dep-mismatch", 99_999, now), good_allocs))
            > 0
        )
        self.assertEqual(verify_money_conservation(bad_dep, good_allocs), [])

        # Negative test: detect DB <-> bank reconciliation mismatch
        with TemporaryDirectory() as directory:
            repo = SqlTransactionRepository(str(Path(directory) / "corrupt.sqlite3"))
            tx_id = repo.create_approved_return(now - timedelta(hours=72))
            service = TransactionService(
                repo, MockDeliveryPort(0), MockBankPort(repo), clock=lambda: now
            )
            service.evaluate(tx_id)
            op = repo.get(tx_id)["operations"][0]
            # Bank succeeded without DB updated to SUCCEEDED
            repo.record_mock_transfer(op)
            strict = InvariantChecker.verify_database(repo, strict_reconciliation=True)
            self.assertFalse(strict.passed)
            self.assertTrue(any("Reconciliation" in v for v in strict.violations))


if __name__ == "__main__":
    unittest.main()
