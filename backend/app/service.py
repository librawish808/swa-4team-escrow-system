"""Transaction orchestration (DCD: B). Carrier I/O never holds a DB transaction."""

import asyncio

from .domain import (
    Action,
    AllocationKind,
    BankPort,
    DeliveryPort,
    DepositAllocation,
    DomainError,
    OperationStatus,
    PolicyContext,
    PolicyRequest,
    ReceiptStatus,
    ShipmentResult,
    TransferAttempt,
    TransferResult,
    TransferStatus,
    timestamp,
    utc_now,
)
from .policy import PolicyController
from .repository import SqlTransactionRepository


class TransactionService:
    def __init__(
        self,
        repository: SqlTransactionRepository,
        delivery: DeliveryPort,
        bank: BankPort,
        clock=utc_now,
        max_attempts=3,
        retry_seconds=1.0,
        policy: PolicyController | None = None,
    ):
        self.repository = repository
        self.delivery = delivery
        self.bank = bank
        self.clock = clock
        self.policy = policy or PolicyController()
        self.max_attempts = max_attempts
        self.retry_seconds = retry_seconds

    def register_return_waybill(self, transaction_id, actor_id, request_id, waybill_no):
        return self.repository.accept_waybill(
            transaction_id, actor_id, request_id, waybill_no, self.clock, self.policy
        )

    def evaluate(self, transaction_id, snapshot=None):
        # A stale pre-receipt read is deliberately supported: CAS failure forces a reread.
        for _ in range(20):
            current = (
                snapshot
                if snapshot is not None
                else self.repository.get(transaction_id)
            )
            now = self.clock()
            policy_decision = self.policy.evaluate_return_deadline(current, now)
            decision = policy_decision.metadata["decision"]
            if self.repository.claim_once(current, decision, now):
                return decision
            snapshot = None
        raise DomainError("거래가 변경되고 있습니다. 다시 처리합니다.", 503)

    def process_due_deadlines(self):
        for transaction_id in self.repository.find_due_deadlines(self.clock()):
            self.evaluate(transaction_id)

    async def verify_receipt(self, receipt):
        try:
            result = await asyncio.wait_for(
                self.delivery.get_shipment_status(
                    receipt["waybill_no"], receipt["seller_id"]
                ),
                timeout=5.0,
            )
        except (TimeoutError, OSError):
            result = ShipmentResult(None, waybill_no=receipt["waybill_no"])
        saved = await asyncio.to_thread(
            self.repository.finish_receipt,
            receipt,
            result,
            self.clock(),
            self.max_attempts,
            self.retry_seconds,
            self.policy,
        )
        if saved:
            await asyncio.to_thread(self.evaluate, receipt["transaction_id"])

    async def _submit_transfer(self, operation: dict) -> TransferResult:
        try:
            if operation["kind"] == "PAYOUT":
                return await asyncio.wait_for(
                    self.bank.request_payout(
                        operation["transaction_id"],
                        operation["request_key"],
                        operation,
                    ),
                    timeout=5.0,
                )
            return await asyncio.wait_for(
                self.bank.request_refund(
                    operation["transaction_id"],
                    operation["request_key"],
                    operation,
                ),
                timeout=5.0,
            )
        except (TimeoutError, OSError):
            return TransferResult(operation["request_key"], TransferStatus.UNKNOWN)

    async def _lookup_transfer(self, request_key: str) -> TransferResult:
        try:
            return await asyncio.wait_for(
                self.bank.get_transfer_result(request_key),
                timeout=5.0,
            )
        except (TimeoutError, OSError):
            return TransferResult(request_key, TransferStatus.UNKNOWN)

    def _evaluate_transfer_result(
        self, operation: dict, result: TransferResult, now
    ):
        return self.policy.evaluate(
            PolicyRequest(
                request_id=operation["request_key"],
                action=Action.HANDLE_TRANSFER_RESULT,
                actor_id=None,
                received_at=now,
                payload={"bank_idempotent": True},
            ),
            PolicyContext(
                now=now,
                transaction_version=operation.get("transaction_version", 0),
                allocation=DepositAllocation(
                    kind=AllocationKind.PRINCIPAL,
                    amount=operation["amount"],
                    operation_id=operation["request_key"],
                ),
                attempt=TransferAttempt(
                    request_key=result.request_key,
                    recipient_account=operation["recipient_id"],
                    status=result.status,
                    bank_transaction_id=result.bank_transaction_id,
                ),
            ),
        )

    @staticmethod
    def _status_from_follow_up(follow_up) -> OperationStatus:
        if Action.COMPLETE_OPERATION in follow_up.actions:
            return OperationStatus.SUCCEEDED
        if Action.ESCALATE_MANUAL_REVIEW in follow_up.actions:
            return OperationStatus.MANUAL_REVIEW
        return OperationStatus.UNKNOWN

    async def dispatch_transfers(self):
        processed: set[str] = set()
        while True:
            operation = await asyncio.to_thread(
                self.repository.claim_operation,
                self.clock(),
                30,
                processed,
            )
            if operation is None:
                break
            processed.add(operation["request_key"])

            if operation["status"] == OperationStatus.UNKNOWN:
                result = await self._lookup_transfer(operation["request_key"])
            else:
                result = await self._submit_transfer(operation)

            follow_up = self._evaluate_transfer_result(
                operation, result, self.clock()
            )
            if Action.REPLAY_TRANSFER_SAME_KEY in follow_up.actions:
                replay_result = await self._submit_transfer(operation)
                follow_up = self._evaluate_transfer_result(
                    operation, replay_result, self.clock()
                )
            next_status = self._status_from_follow_up(follow_up)

            await asyncio.to_thread(
                self.repository.save_transfer_result,
                operation["request_key"],
                next_status,
                self.clock(),
                lease_token=operation["lease_token"],
                expected_version=operation["transaction_version"],
            )

    def mark_mock_arrived(self, transaction_id):
        self.repository.mark_mock_arrived(transaction_id, self.clock(), self.policy)
        self.evaluate(transaction_id)

    def get_transaction_status(self, transaction_id, actor_id=None):
        value = self.repository.get(transaction_id)
        if actor_id is not None:
            decision = self.policy.validate_read(value, actor_id, now=self.clock())
            if not decision.approved:
                raise DomainError(decision.reason, decision.status_code)
        receipts = value["receipts"]
        operations = value["operations"]
        label, status, held = "운송장 미접수", "RETURN_APPROVED", True
        if any(r["status"] == ReceiptStatus.MANUAL_REVIEW for r in receipts):
            label, status = "담당자 확인 필요", "MANUAL_REVIEW"
        elif any(
            r["status"] in (ReceiptStatus.PENDING, ReceiptStatus.UNKNOWN)
            for r in receipts
        ):
            label, status = "운송장 확인 중", "RETURN_VERIFYING"
        if any(r["status"] == ReceiptStatus.VALID for r in receipts):
            label, status = "반품 진행", "RETURN_IN_TRANSIT"
        if any(r["status"] == ReceiptStatus.DELIVERED for r in receipts):
            label, status = "반품 도착 · 환불 확인 중", "REFUND_PROCESSING"
        if operations:
            operation = operations[0]
            held = False
            if operation["kind"] == "PAYOUT":
                label, status = "판매자 지급 확인 중", "PAYOUT_PROCESSING"
                if operation["status"] == OperationStatus.SUCCEEDED:
                    label, status = "판매자 지급 완료", "COMPLETED"
                elif operation["status"] == OperationStatus.MANUAL_REVIEW:
                    label, status = "지급 실패 · 담당자 확인 필요", "MANUAL_REVIEW"
            else:
                if operation["status"] == OperationStatus.SUCCEEDED:
                    label, status = "환불 완료", "CANCELLED"
                elif operation["status"] == OperationStatus.MANUAL_REVIEW:
                    label, status = "환불 실패 · 담당자 확인 필요", "MANUAL_REVIEW"
        return {
            "id": value["id"],
            "amount": value["amount"],
            "deadline": value["deadline"],
            "server_now": timestamp(self.clock()),
            "status": status,
            "return_label": label,
            "payment_held": held,
            "version": value["version"],
            "receipts": [
                {
                    k: r[k]
                    for k in ("id", "waybill_no", "status", "received_at", "attempts")
                }
                for r in receipts
            ],
            "operations": [
                {k: o[k] for k in ("kind", "status", "amount", "request_key")}
                for o in operations
            ],
            "events": value["events"],
        }
