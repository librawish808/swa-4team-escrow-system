"""Transaction orchestration (DCD: B). Carrier I/O never holds a DB transaction."""

import asyncio

from .domain import (
    BankPort,
    DeliveryPort,
    DomainError,
    PolicyController,
    ReceiptStatus,
    ShipmentResult,
    timestamp,
    utc_now,
)
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
    ):
        self.repository = repository
        self.delivery = delivery
        self.bank = bank
        self.clock = clock
        self.policy = PolicyController()
        self.max_attempts = max_attempts
        self.retry_seconds = retry_seconds

    def register_return_waybill(self, transaction_id, actor_id, request_id, waybill_no):
        return self.repository.accept_waybill(
            transaction_id, actor_id, request_id, waybill_no, self.clock
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
            decision = self.policy.evaluate(current, now)
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
            result = ShipmentResult(None)
        saved = await asyncio.to_thread(
            self.repository.finish_receipt,
            receipt,
            result,
            self.clock(),
            self.max_attempts,
            self.retry_seconds,
        )
        if saved:
            await asyncio.to_thread(self.evaluate, receipt["transaction_id"])

    async def dispatch_transfers(self):
        operations = await asyncio.to_thread(self.repository.pending_operations)
        for operation in operations:
            status = await self.bank.transfer(operation)
            await asyncio.to_thread(
                self.repository.save_transfer_result,
                operation["request_key"],
                status,
                self.clock(),
            )

    def mark_mock_arrived(self, transaction_id):
        self.repository.mark_mock_arrived(transaction_id, self.clock())
        self.evaluate(transaction_id)

    def get_transaction_status(self, transaction_id, actor_id=None):
        value = self.repository.get(transaction_id)
        if actor_id is not None and actor_id not in (
            value["buyer_id"],
            value["seller_id"],
        ):
            raise DomainError("거래 참여자만 조회할 수 있습니다.", 403)
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
                if operation["status"] == "SUCCEEDED":
                    label, status = "판매자 지급 완료", "COMPLETED"
            elif operation["status"] == "SUCCEEDED":
                label, status = "환불 완료", "CANCELLED"
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
