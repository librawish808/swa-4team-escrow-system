import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from app.adapters import MockBankPort, MockDeliveryPort
from app.domain import (
    Action,
    AllocationKind,
    DecisionType,
    Deposit,
    DepositAllocation,
    LookupStatus,
    OperationStatus,
    OperationType,
    PolicyContext,
    PolicyRequest,
    ReceiptStatus,
    ShipmentResult,
    TransactionStatus,
    TransferAttempt,
    TransferResult,
    TransferStatus,
)
from app.policy import (
    DeadlinePolicy,
    DepositPolicy,
    FulfillmentPolicy,
    FundsPolicy,
    PolicyController,
    ReturnPolicy,
)
from app.repository import SqlTransactionRepository
from app.service import TransactionService


class PolicyAlignmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.policy = PolicyController()
        self.now = datetime(2026, 11, 26, 9, 0, tzinfo=UTC)

    def test_deadline_policy_supports_custom_period_and_is_due(self):
        start = self.now
        self.assertEqual(
            DeadlinePolicy.deadline_of(start), start + timedelta(hours=72)
        )
        three_days = DeadlinePolicy.deadline_of(
            start, DeadlinePolicy.DEFAULT_DEPOSIT_PERIOD
        )
        self.assertEqual(three_days, start + timedelta(days=3))
        self.assertTrue(
            DeadlinePolicy.is_before_deadline(
                three_days - timedelta(milliseconds=1), three_days
            )
        )
        self.assertFalse(DeadlinePolicy.is_before_deadline(three_days, three_days))
        self.assertTrue(DeadlinePolicy.is_due(three_days, three_days))
        self.assertFalse(
            DeadlinePolicy.is_due(three_days - timedelta(milliseconds=1), three_days)
        )

    def test_transaction_policy_validate_methods_delegate_to_evaluate(self):
        tx = {
            "id": "tx-1",
            "seller_id": "seller-1",
            "buyer_id": "buyer-1",
            "invite_token": "tok-123",
            "status": TransactionStatus.WAITING_BUYER,
            "version": 2,
        }
        # validate_join
        self.assertTrue(self.policy.validate_join(tx, "buyer-1", "tok-123").approved)
        bad_token = self.policy.validate_join(tx, "buyer-1", "wrong")
        self.assertEqual(bad_token.verdict, DecisionType.REJECT)
        self.assertEqual(bad_token.status_code, 403)
        self_join = self.policy.validate_join(tx, "seller-1", "tok-123")
        self.assertEqual(self_join.verdict, DecisionType.REJECT)

        # validate_read
        self.assertTrue(self.policy.validate_read(tx, "buyer-1").approved)
        self.assertTrue(self.policy.validate_read(tx, "seller-1").approved)
        outsider = self.policy.validate_read(tx, "outsider")
        self.assertEqual(outsider.verdict, DecisionType.REJECT)
        self.assertEqual(outsider.status_code, 403)

        # validate_waybill
        tx["status"] = TransactionStatus.DEPOSIT_CONFIRMED
        self.assertTrue(self.policy.validate_waybill(tx, "seller-1").approved)
        self.assertEqual(
            self.policy.validate_waybill(tx, "buyer-1").status_code, 403
        )

        # validate_purchase
        tx["status"] = TransactionStatus.WAYBILL_REGISTERED
        tx["waybill_no"] = "WB-001"
        self.assertTrue(self.policy.validate_purchase(tx, "buyer-1").approved)
        tx["payment_held"] = True
        held = self.policy.validate_purchase(tx, "buyer-1")
        self.assertEqual(held.verdict, DecisionType.HOLD)

        # validate_cancellation
        tx["payment_held"] = False
        self.assertEqual(
            self.policy.validate_cancellation(tx, "buyer-1").verdict,
            DecisionType.REJECT,
        )
        tx["status"] = TransactionStatus.DEPOSIT_CONFIRMED
        tx["waybill_no"] = None
        cancel_refund = self.policy.validate_cancellation(tx, "buyer-1")
        self.assertTrue(cancel_refund.approved)
        self.assertIn(Action.START_REFUND, cancel_refund.actions)

    def test_deposit_policy_normal_shortfall_excess_duplicate_and_late(self):
        tx = {
            "id": "tx-1",
            "amount": 100_000,
            "status": TransactionStatus.WAITING_DEPOSIT,
            "deposit_deadline": self.now + timedelta(days=3),
            "version": 1,
        }
        ctx = PolicyContext(now=self.now, transaction_version=1, transaction=tx)

        # Normal
        normal = DepositPolicy.evaluate_deposit(
            Deposit("dep-1", 100_000, self.now), ctx
        )
        self.assertTrue(normal.approved)
        self.assertEqual(normal.actions, (Action.CONFIRM_DEPOSIT,))
        self.assertEqual(
            [a.kind for a in normal.metadata["allocations"]],
            [AllocationKind.PRINCIPAL],
        )

        # Shortfall
        short = DepositPolicy.evaluate_deposit(
            Deposit("dep-2", 90_000, self.now), ctx
        )
        self.assertEqual(short.verdict, DecisionType.HOLD)
        self.assertEqual(short.metadata["shortfall"], 10_000)

        # Overpaid -> PRINCIPAL + EXCESS
        excess = DepositPolicy.evaluate_deposit(
            Deposit("dep-3", 120_000, self.now), ctx
        )
        self.assertTrue(excess.approved)
        self.assertEqual(
            excess.actions, (Action.CONFIRM_DEPOSIT, Action.REFUND_EXCESS)
        )
        self.assertEqual(
            [(a.kind, a.amount) for a in excess.metadata["allocations"]],
            [(AllocationKind.PRINCIPAL, 100_000), (AllocationKind.EXCESS, 20_000)],
        )

        # Late deposit -> UNAPPLIED
        late = DepositPolicy.evaluate_deposit(
            Deposit("dep-4", 100_000, self.now + timedelta(days=3)), ctx
        )
        self.assertEqual(late.actions, (Action.REFUND_UNAPPLIED,))
        self.assertEqual(
            late.metadata["allocations"][0].kind, AllocationKind.UNAPPLIED
        )

        # Duplicate / non-waiting -> UNAPPLIED
        tx_done = {**tx, "status": TransactionStatus.DEPOSIT_CONFIRMED}
        dup = DepositPolicy.evaluate_deposit(
            Deposit("dep-5", 100_000, self.now),
            PolicyContext(now=self.now, transaction_version=2, transaction=tx_done),
        )
        self.assertEqual(dup.actions, (Action.REFUND_UNAPPLIED,))

    def test_fulfillment_and_cancellation_auto_policies(self):
        delivered_at = self.now - timedelta(days=7)
        tx = {
            "id": "tx-2",
            "seller_id": "s1",
            "buyer_id": "b1",
            "status": TransactionStatus.WAYBILL_REGISTERED,
            "version": 3,
        }
        # Auto-confirm at 7 days
        auto_ok = FulfillmentPolicy.evaluate_confirmation(
            PolicyRequest("auto-1", Action.AUTO_CONFIRM_PURCHASE, None, self.now),
            PolicyContext(
                now=self.now,
                transaction_version=3,
                waybill_registered=True,
                delivered_at=delivered_at,
                transaction=tx,
            ),
        )
        self.assertTrue(auto_ok.approved)
        self.assertIn(Action.START_PAYOUT, auto_ok.actions)

        # Auto-confirm before 7 days -> WAIT
        auto_wait = FulfillmentPolicy.evaluate_confirmation(
            PolicyRequest("auto-2", Action.AUTO_CONFIRM_PURCHASE, None, self.now),
            PolicyContext(
                now=self.now - timedelta(seconds=1),
                transaction_version=3,
                waybill_registered=True,
                delivered_at=delivered_at,
                transaction=tx,
            ),
        )
        self.assertEqual(auto_wait.verdict, DecisionType.WAIT)

        # Auto-cancel 3d shipment timeout -> START_REFUND
        tx_paid = {
            "id": "tx-3",
            "status": TransactionStatus.DEPOSIT_CONFIRMED,
            "shipment_deadline": self.now,
            "version": 2,
        }
        auto_cancel = self.policy.evaluate(
            PolicyRequest("ac-1", Action.AUTO_CANCEL_TRANSACTION, None, self.now),
            PolicyContext(
                now=self.now,
                transaction_version=2,
                waybill_registered=False,
                transaction=tx_paid,
            ),
        )
        self.assertTrue(auto_cancel.approved)
        self.assertIn(Action.START_REFUND, auto_cancel.actions)

    def test_return_issue_and_shipment_result_policy(self):
        tx = {
            "id": "tx-4",
            "buyer_id": "b1",
            "seller_id": "s1",
            "status": TransactionStatus.WAYBILL_REGISTERED,
            "version": 4,
        }
        issue = ReturnPolicy.evaluate_issue(
            PolicyRequest("iss-1", Action.RAISE_ISSUE, "b1", self.now),
            PolicyContext(
                now=self.now,
                transaction_version=4,
                delivered_at=self.now - timedelta(days=2),
                transaction=tx,
            ),
        )
        self.assertEqual(issue.verdict, DecisionType.HOLD)
        self.assertIn(Action.HOLD_PAYOUT, issue.actions)

        self.assertEqual(
            ReturnPolicy.evaluate_shipment_result(
                ShipmentResult(None), "s1", 0, 3
            ),
            ReceiptStatus.UNKNOWN,
        )
        self.assertEqual(
            ReturnPolicy.evaluate_shipment_result(
                ShipmentResult(None), "s1", 2, 3
            ),
            ReceiptStatus.MANUAL_REVIEW,
        )
        self.assertEqual(
            ReturnPolicy.evaluate_shipment_result(
                ShipmentResult(True, "s1", delivered=False), "s1", 0, 3
            ),
            ReceiptStatus.VALID,
        )
        self.assertEqual(
            ReturnPolicy.evaluate_shipment_result(
                ShipmentResult(True, "other"), "s1", 0, 3
            ),
            ReceiptStatus.INVALID,
        )

    def test_funds_policy_mutual_exclusion_excess_and_transfer_statuses(self):
        principal = DepositAllocation(AllocationKind.PRINCIPAL, 100_000, "dep-1")
        excess = DepositAllocation(AllocationKind.EXCESS, 10_000, "dep-1")
        unapplied = DepositAllocation(AllocationKind.UNAPPLIED, 50_000, "dep-2")

        # Principal blocked when operation already exists
        blocked = FundsPolicy.evaluate_operation(
            principal,
            PolicyRequest(
                "op-1",
                Action.EXECUTE_FUNDS_OPERATION,
                None,
                self.now,
                payload={"operation_type": OperationType.PAYOUT},
            ),
            PolicyContext(
                now=self.now,
                transaction={"operations": [{"kind": "REFUND"}]},
            ),
        )
        self.assertEqual(blocked.verdict, DecisionType.REJECT)

        # Excess approved even if transaction is COMPLETED
        excess_ok = FundsPolicy.evaluate_operation(
            excess,
            PolicyRequest("op-2", Action.EXECUTE_FUNDS_OPERATION, None, self.now),
            PolicyContext(
                now=self.now,
                transaction={"status": TransactionStatus.COMPLETED, "operations": [{}]},
            ),
        )
        self.assertTrue(excess_ok.approved)
        self.assertIn(Action.REFUND_EXCESS, excess_ok.actions)

        # Unapplied approved even if transaction is CANCELLED
        unapplied_ok = FundsPolicy.evaluate_operation(
            unapplied,
            PolicyRequest("op-3", Action.EXECUTE_FUNDS_OPERATION, None, self.now),
            PolicyContext(
                now=self.now,
                transaction={"status": TransactionStatus.CANCELLED},
            ),
        )
        self.assertTrue(unapplied_ok.approved)
        self.assertIn(Action.REFUND_UNAPPLIED, unapplied_ok.actions)

        # TransferStatus follow-ups
        for status, expected_action in (
            (TransferStatus.SUCCEEDED, Action.COMPLETE_OPERATION),
            (TransferStatus.UNKNOWN, Action.RETRY_TRANSFER_LOOKUP),
            (TransferStatus.NOT_FOUND, Action.REPLAY_TRANSFER_SAME_KEY),
            (TransferStatus.FAILED, Action.ESCALATE_MANUAL_REVIEW),
        ):
            decision = FundsPolicy.evaluate_operation(
                principal,
                PolicyRequest(
                    "res-1",
                    Action.HANDLE_TRANSFER_RESULT,
                    None,
                    self.now,
                    payload={"bank_idempotent": True},
                ),
                PolicyContext(
                    now=self.now,
                    allocation=principal,
                    attempt=TransferAttempt("k1", "acc-1", status),
                ),
            )
            self.assertIn(expected_action, decision.actions)

    async def test_bank_unknown_recovers_via_get_transfer_result_and_failed_escalates(
        self,
    ):
        class FlakyBankPort(MockBankPort):
            def __init__(self, repo):
                super().__init__(repo)
                self.mode = "UNKNOWN_AFTER_EFFECT"
                self.lookup_calls = 0

            async def request_payout(self, transaction_id, request_key, operation=None):
                if self.mode == "UNKNOWN_AFTER_EFFECT":
                    self.repository.record_mock_transfer(operation)
                    return TransferResult(request_key, TransferStatus.UNKNOWN)
                if self.mode == "FAILED":
                    return TransferResult(request_key, TransferStatus.FAILED)
                return await super().request_payout(
                    transaction_id, request_key, operation
                )

            async def get_transfer_result(self, request_key):
                self.lookup_calls += 1
                return await super().get_transfer_result(request_key)

        with TemporaryDirectory() as directory:
            repo = SqlTransactionRepository(str(Path(directory) / "bank.sqlite3"))
            bank = FlakyBankPort(repo)
            deadline = self.now
            service = TransactionService(
                repo, MockDeliveryPort(0), bank, clock=lambda: self.now
            )

            # Case 1: UNKNOWN response after bank effect -> stays UNKNOWN, then get_transfer_result confirms SUCCEEDED without duplicate transfer
            tx1 = repo.create_approved_return(deadline - timedelta(hours=72))
            service.evaluate(tx1)
            await service.dispatch_transfers()
            self.assertEqual(
                repo.get(tx1)["operations"][0]["status"], OperationStatus.UNKNOWN
            )
            await service.dispatch_transfers()
            self.assertEqual(bank.lookup_calls, 1)
            self.assertEqual(
                repo.get(tx1)["operations"][0]["status"], OperationStatus.SUCCEEDED
            )
            self.assertEqual(repo.transfer_count("PAYOUT"), 1)

            # Case 2: FAILED response -> FundsPolicy escalates to MANUAL_REVIEW
            bank.mode = "FAILED"
            tx2 = repo.create_approved_return(deadline - timedelta(hours=72))
            service.evaluate(tx2)
            await service.dispatch_transfers()
            self.assertEqual(
                repo.get(tx2)["operations"][0]["status"], OperationStatus.MANUAL_REVIEW
            )
            self.assertEqual(
                service.get_transaction_status(tx2)["status"], "MANUAL_REVIEW"
            )
            self.assertEqual(ShipmentResult(None).status, LookupStatus.UNKNOWN)

    async def test_bank_timeout_transitions_to_unknown_and_not_found_replays_same_key(
        self,
    ):
        class TimeoutThenRecoverBankPort(MockBankPort):
            def __init__(self, repo):
                super().__init__(repo)
                self.submit_calls = 0
                self.lookup_calls = 0
                self.fail_once = True

            async def request_payout(self, transaction_id, request_key, operation=None):
                self.submit_calls += 1
                if self.fail_once:
                    self.fail_once = False
                    raise TimeoutError("bank timeout")
                return await super().request_payout(
                    transaction_id, request_key, operation
                )

            async def get_transfer_result(self, request_key):
                self.lookup_calls += 1
                return await super().get_transfer_result(request_key)

        with TemporaryDirectory() as directory:
            repo = SqlTransactionRepository(str(Path(directory) / "timeout.sqlite3"))
            bank = TimeoutThenRecoverBankPort(repo)
            service = TransactionService(
                repo, MockDeliveryPort(0), bank, clock=lambda: self.now
            )
            tx = repo.create_approved_return(self.now - timedelta(hours=72))
            service.evaluate(tx)

            # Tick 1: TimeoutError -> transitions to UNKNOWN (not left as PENDING)
            await service.dispatch_transfers()
            self.assertEqual(bank.submit_calls, 1)
            self.assertEqual(bank.lookup_calls, 0)
            self.assertEqual(
                repo.get(tx)["operations"][0]["status"], OperationStatus.UNKNOWN
            )

            # Tick 2: UNKNOWN -> get_transfer_result() returns NOT_FOUND -> replays same key -> SUCCEEDED
            await service.dispatch_transfers()
            self.assertEqual(bank.lookup_calls, 1)
            self.assertEqual(bank.submit_calls, 2)
            self.assertEqual(
                repo.get(tx)["operations"][0]["status"], OperationStatus.SUCCEEDED
            )
            self.assertEqual(repo.transfer_count("PAYOUT"), 1)

    def test_operation_lease_and_expected_version_guard_and_zero_deposit_rejection(
        self,
    ):
        # Deposit <= 0 rejected even on CANCELLED transaction
        cancelled_tx = {
            "id": "tx-cancel",
            "amount": 100_000,
            "status": TransactionStatus.CANCELLED,
            "version": 1,
        }
        zero_dep = DepositPolicy.evaluate_deposit(
            Deposit("dep-0", 0, self.now),
            PolicyContext(now=self.now, transaction_version=1, transaction=cancelled_tx),
        )
        self.assertEqual(zero_dep.verdict, DecisionType.REJECT)
        self.assertEqual(zero_dep.status_code, 400)

        # Deterministic now injection on TransactionPolicy.validate_waybill
        tx_ship = {
            "id": "tx-ship",
            "seller_id": "s1",
            "status": TransactionStatus.DEPOSIT_CONFIRMED,
            "shipment_deadline": self.now,
            "version": 2,
        }
        self.assertTrue(
            self.policy.validate_waybill(
                tx_ship, "s1", now=self.now - timedelta(seconds=1)
            ).approved
        )
        self.assertEqual(
            self.policy.validate_waybill(tx_ship, "s1", now=self.now).verdict,
            DecisionType.REJECT,
        )

        # Operation lease claim + expected_version check
        with TemporaryDirectory() as directory:
            repo = SqlTransactionRepository(str(Path(directory) / "lease.sqlite3"))
            service = TransactionService(
                repo, MockDeliveryPort(0), MockBankPort(repo), clock=lambda: self.now
            )
            tx_id = repo.create_approved_return(self.now - timedelta(hours=72))
            service.evaluate(tx_id)

            claimed = repo.claim_operation(self.now, lease_seconds=10)
            self.assertIsNotNone(claimed)
            self.assertIsNone(repo.claim_operation(self.now))

            # Stale expected_version fails
            self.assertFalse(
                repo.save_transfer_result(
                    claimed["request_key"],
                    OperationStatus.SUCCEEDED,
                    self.now,
                    lease_token=claimed["lease_token"],
                    expected_version=claimed["transaction_version"] + 99,
                )
            )
            # Wrong lease_token fails
            self.assertFalse(
                repo.save_transfer_result(
                    claimed["request_key"],
                    OperationStatus.SUCCEEDED,
                    self.now,
                    lease_token="wrong-token",
                    expected_version=claimed["transaction_version"],
                )
            )
            # Matching lease_token and expected_version succeeds
            self.assertTrue(
                repo.save_transfer_result(
                    claimed["request_key"],
                    OperationStatus.SUCCEEDED,
                    self.now,
                    lease_token=claimed["lease_token"],
                    expected_version=claimed["transaction_version"],
                )
            )


if __name__ == "__main__":
    unittest.main()
