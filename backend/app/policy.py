"""Pure business policy evaluation (DCD: C. Policy & Exception).

C decides approve/reject/hold/wait conditions without performing any DB writes,
locks, or external network I/O.
"""

from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4

from .domain import (
    Action,
    AllocationKind,
    Decision,
    DecisionType,
    Deposit,
    DepositAllocation,
    OperationType,
    PolicyContext,
    PolicyDecision,
    PolicyRequest,
    ReceiptStatus,
    ShipmentResult,
    TransactionStatus,
    TransferStatus,
    instant,
    utc_now,
)


def _decision_id() -> str:
    return str(uuid4())


class TransactionPolicy(Protocol):
    """Integrated DCD TransactionPolicy contract."""

    def validate_join(
        self,
        transaction: dict,
        buyer_id: str,
        token: str,
        now: datetime | None = None,
    ) -> PolicyDecision: ...

    def validate_waybill(
        self,
        transaction: dict,
        seller_id: str,
        now: datetime | None = None,
    ) -> PolicyDecision: ...

    def validate_purchase(
        self,
        transaction: dict,
        buyer_id: str,
        now: datetime | None = None,
    ) -> PolicyDecision: ...

    def validate_cancellation(
        self,
        transaction: dict,
        actor_id: str,
        now: datetime | None = None,
    ) -> PolicyDecision: ...

    def validate_read(
        self,
        transaction: dict,
        actor_id: str,
        now: datetime | None = None,
    ) -> PolicyDecision: ...


class DeadlinePolicy:
    DEFAULT_RETURN_PERIOD = timedelta(hours=72)
    DEFAULT_DEPOSIT_PERIOD = timedelta(days=3)
    DEFAULT_SHIPMENT_PERIOD = timedelta(days=3)
    DEFAULT_AUTO_CONFIRM_PERIOD = timedelta(days=7)

    @staticmethod
    def deadline_of(
        start: datetime, period: timedelta = DEFAULT_RETURN_PERIOD
    ) -> datetime:
        return start + period

    @staticmethod
    def is_before_deadline(at: datetime, deadline: datetime) -> bool:
        return at < deadline

    @staticmethod
    def is_due(at: datetime, deadline: datetime) -> bool:
        return at >= deadline


class DepositPolicy:
    """Evaluates normal, underpaid, overpaid, duplicate, and late deposits."""

    @staticmethod
    def evaluate_deposit(deposit: Deposit, context: PolicyContext) -> PolicyDecision:
        tx = context.transaction or {}
        version = context.transaction_version
        expected_amount = tx.get("amount", 0)
        status = tx.get("status", TransactionStatus.WAITING_DEPOSIT)
        deposit_deadline = tx.get("deposit_deadline")

        if deposit.received_amount <= 0:
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="입금 금액은 0보다 커야 합니다.",
                expected_version=version,
                status_code=400,
                metadata={"allocations": []},
            )

        if status != TransactionStatus.WAITING_DEPOSIT:
            allocation = DepositAllocation(
                kind=AllocationKind.UNAPPLIED,
                amount=deposit.received_amount,
                bank_deposit_id=deposit.bank_deposit_id,
            )
            return PolicyDecision(
                verdict=DecisionType.HOLD,
                decision_id=_decision_id(),
                reason="입금 대기 상태가 아닌 거래의 입금은 미배분으로 기록하고 반환합니다.",
                actions=(Action.REFUND_UNAPPLIED,),
                expected_version=version,
                metadata={"allocations": [allocation]},
            )

        if deposit_deadline is not None:
            deadline_dt = (
                instant(deposit_deadline)
                if isinstance(deposit_deadline, str)
                else deposit_deadline
            )
            if DeadlinePolicy.is_due(deposit.bank_deposited_at, deadline_dt):
                allocation = DepositAllocation(
                    kind=AllocationKind.UNAPPLIED,
                    amount=deposit.received_amount,
                    bank_deposit_id=deposit.bank_deposit_id,
                )
                return PolicyDecision(
                    verdict=DecisionType.HOLD,
                    decision_id=_decision_id(),
                    reason="입금 기한 경과 후 도착한 입금은 미배분으로 반환합니다.",
                    actions=(Action.REFUND_UNAPPLIED,),
                    expected_version=version,
                    metadata={"allocations": [allocation]},
                )

        if deposit.received_amount < expected_amount:
            return PolicyDecision(
                verdict=DecisionType.HOLD,
                decision_id=_decision_id(),
                reason="입금 금액이 거래 금액보다 부족합니다.",
                actions=(Action.WAIT,),
                expected_version=version,
                metadata={
                    "allocations": [],
                    "shortfall": expected_amount - deposit.received_amount,
                },
            )

        if deposit.received_amount == expected_amount:
            principal = DepositAllocation(
                kind=AllocationKind.PRINCIPAL,
                amount=expected_amount,
                bank_deposit_id=deposit.bank_deposit_id,
            )
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="정상 입금이 확인되었습니다.",
                actions=(Action.CONFIRM_DEPOSIT,),
                expected_version=version,
                metadata={"allocations": [principal]},
            )

        principal = DepositAllocation(
            kind=AllocationKind.PRINCIPAL,
            amount=expected_amount,
            bank_deposit_id=deposit.bank_deposit_id,
        )
        excess = DepositAllocation(
            kind=AllocationKind.EXCESS,
            amount=deposit.received_amount - expected_amount,
            bank_deposit_id=deposit.bank_deposit_id,
        )
        return PolicyDecision(
            verdict=DecisionType.APPROVE,
            decision_id=_decision_id(),
            reason="거래 금액을 초과 입금하여 원금을 인정하고 초과금을 별도 반환합니다.",
            actions=(Action.CONFIRM_DEPOSIT, Action.REFUND_EXCESS),
            expected_version=version,
            metadata={"allocations": [principal, excess]},
        )


class FulfillmentPolicy:
    """Evaluates seller shipment registration and manual/automatic purchase confirmation."""

    @staticmethod
    def evaluate_shipment(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        tx = context.transaction or {}
        version = context.transaction_version
        if request.actor_id != tx.get("seller_id"):
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="판매자만 발송 운송장을 등록할 수 있습니다.",
                expected_version=version,
                status_code=403,
            )
        if tx.get("status") != TransactionStatus.DEPOSIT_CONFIRMED:
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="입금 확인 상태에서만 발송 운송장을 등록할 수 있습니다.",
                expected_version=version,
                status_code=409,
            )
        shipment_deadline = tx.get("shipment_deadline")
        if shipment_deadline is not None:
            deadline_dt = (
                instant(shipment_deadline)
                if isinstance(shipment_deadline, str)
                else shipment_deadline
            )
            if not DeadlinePolicy.is_before_deadline(request.received_at, deadline_dt):
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="발송 운송장 등록 기한이 지났습니다.",
                    expected_version=version,
                    status_code=409,
                )
        return PolicyDecision(
            verdict=DecisionType.APPROVE,
            decision_id=_decision_id(),
            reason="발송 운송장 등록이 승인되었습니다.",
            actions=(Action.REGISTER_WAYBILL,),
            expected_version=version,
        )

    @staticmethod
    def evaluate_confirmation(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        tx = context.transaction or {}
        version = context.transaction_version
        if context.payment_held:
            return PolicyDecision(
                verdict=DecisionType.HOLD,
                decision_id=_decision_id(),
                reason="이의 제기 또는 반품으로 지급이 보류되어 구매확정할 수 없습니다.",
                actions=(Action.HOLD_PAYOUT,),
                expected_version=version,
                status_code=409,
            )
        if tx.get("status") != TransactionStatus.WAYBILL_REGISTERED and not context.waybill_registered:
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="발송 등록된 거래만 구매확정할 수 있습니다.",
                expected_version=version,
                status_code=409,
            )
        if request.action == Action.AUTO_CONFIRM_PURCHASE:
            if context.delivered_at is None:
                return PolicyDecision(
                    verdict=DecisionType.WAIT,
                    decision_id=_decision_id(),
                    reason="배송 완료 시각이 확인되지 않아 자동 구매확정을 대기합니다.",
                    actions=(Action.WAIT,),
                    expected_version=version,
                )
            auto_deadline = DeadlinePolicy.deadline_of(
                context.delivered_at, DeadlinePolicy.DEFAULT_AUTO_CONFIRM_PERIOD
            )
            if DeadlinePolicy.is_due(context.now, auto_deadline):
                return PolicyDecision(
                    verdict=DecisionType.APPROVE,
                    decision_id=_decision_id(),
                    reason="배송 완료 후 7일이 경과하여 자동 구매확정 및 판매자 지급을 승인합니다.",
                    actions=(Action.START_PAYOUT,),
                    expected_version=version,
                )
            return PolicyDecision(
                verdict=DecisionType.WAIT,
                decision_id=_decision_id(),
                reason="자동 구매확정 기한이 아직 도래하지 않았습니다.",
                actions=(Action.WAIT,),
                expected_version=version,
            )

        if request.actor_id != tx.get("buyer_id"):
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="구매자만 수동 구매확정할 수 있습니다.",
                expected_version=version,
                status_code=403,
            )
        return PolicyDecision(
            verdict=DecisionType.APPROVE,
            decision_id=_decision_id(),
            reason="구매확정이 승인되어 판매자 지급을 진행합니다.",
            actions=(Action.START_PAYOUT,),
            expected_version=version,
        )


class CancellationPolicy:
    """Evaluates manual buyer cancellation and automatic timeout cancellation."""

    @staticmethod
    def evaluate_cancellation(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        tx = context.transaction or {}
        version = context.transaction_version
        status = tx.get("status")

        if request.action == Action.AUTO_CANCEL_TRANSACTION:
            if status == TransactionStatus.WAITING_DEPOSIT:
                deadline = tx.get("deposit_deadline")
                if deadline is not None:
                    deadline_dt = instant(deadline) if isinstance(deadline, str) else deadline
                    if DeadlinePolicy.is_due(context.now, deadline_dt):
                        return PolicyDecision(
                            verdict=DecisionType.APPROVE,
                            decision_id=_decision_id(),
                            reason="입금 기한 3일이 경과하여 자동 취소합니다.",
                            actions=(Action.CANCEL_TRANSACTION,),
                            expected_version=version,
                        )
                return PolicyDecision(
                    verdict=DecisionType.WAIT,
                    decision_id=_decision_id(),
                    reason="입금 기한이 아직 남았습니다.",
                    actions=(Action.WAIT,),
                    expected_version=version,
                )
            if status == TransactionStatus.DEPOSIT_CONFIRMED and not context.waybill_registered:
                deadline = tx.get("shipment_deadline")
                if deadline is not None:
                    deadline_dt = instant(deadline) if isinstance(deadline, str) else deadline
                    if DeadlinePolicy.is_due(context.now, deadline_dt):
                        return PolicyDecision(
                            verdict=DecisionType.APPROVE,
                            decision_id=_decision_id(),
                            reason="발송 기한 3일이 경과하여 자동 취소 및 전액 환불을 승인합니다.",
                            actions=(Action.START_REFUND,),
                            expected_version=version,
                        )
                return PolicyDecision(
                    verdict=DecisionType.WAIT,
                    decision_id=_decision_id(),
                    reason="발송 기한이 아직 남았습니다.",
                    actions=(Action.WAIT,),
                    expected_version=version,
                )
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="자동 취소 대상 상태가 아닙니다.",
                expected_version=version,
                status_code=409,
            )

        if request.actor_id not in (tx.get("buyer_id"), tx.get("seller_id")):
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="거래 참여자만 취소를 요청할 수 있습니다.",
                expected_version=version,
                status_code=403,
            )
        if context.waybill_registered or status not in (
            TransactionStatus.WAITING_BUYER,
            TransactionStatus.WAITING_DEPOSIT,
            TransactionStatus.DEPOSIT_CONFIRMED,
        ):
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="발송 등록 이후에는 일반 취소를 할 수 없습니다.",
                expected_version=version,
                status_code=409,
            )
        if status == TransactionStatus.DEPOSIT_CONFIRMED:
            if request.actor_id != tx.get("buyer_id"):
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="입금 확인 후 취소는 구매자만 요청할 수 있습니다.",
                    expected_version=version,
                    status_code=403,
                )
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="발송 전 취소로 전액 환불을 승인합니다.",
                actions=(Action.START_REFUND,),
                expected_version=version,
            )
        return PolicyDecision(
            verdict=DecisionType.APPROVE,
            decision_id=_decision_id(),
            reason="입금 전 거래 취소를 승인합니다.",
            actions=(Action.CANCEL_TRANSACTION,),
            expected_version=version,
        )


class ReturnPolicy:
    """Evaluates issue/hold, return waybill acceptance, carrier verification, and deadlines."""

    @staticmethod
    def evaluate_issue(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        tx = context.transaction or {}
        version = context.transaction_version
        if request.actor_id != tx.get("buyer_id"):
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="구매자만 이의를 제기할 수 있습니다.",
                expected_version=version,
                status_code=403,
            )
        if tx.get("status") in (
            TransactionStatus.COMPLETED,
            TransactionStatus.CANCELLED,
            TransactionStatus.PAYOUT_PROCESSING,
            TransactionStatus.REFUND_PROCESSING,
        ):
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="이미 정산 또는 취소가 진행·완료된 거래에는 이의를 제기할 수 없습니다.",
                expected_version=version,
                status_code=409,
            )
        if context.delivered_at is not None:
            issue_deadline = DeadlinePolicy.deadline_of(
                context.delivered_at, DeadlinePolicy.DEFAULT_AUTO_CONFIRM_PERIOD
            )
            if not DeadlinePolicy.is_before_deadline(request.received_at, issue_deadline):
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="이의 제기 기한이 지났습니다.",
                    expected_version=version,
                    status_code=409,
                )
        return PolicyDecision(
            verdict=DecisionType.HOLD,
            decision_id=_decision_id(),
            reason="이의 제기가 접수되어 판매자 지급을 보류합니다.",
            actions=(Action.HOLD_PAYOUT,),
            expected_version=version,
        )

    @staticmethod
    def evaluate_shipment_result(
        result: ShipmentResult,
        seller_id: str,
        current_attempts: int,
        max_attempts: int,
    ) -> ReceiptStatus:
        attempts = current_attempts + 1
        if result.valid is None:
            return (
                ReceiptStatus.MANUAL_REVIEW
                if attempts >= max_attempts
                else ReceiptStatus.UNKNOWN
            )
        if result.valid and result.recipient_id == seller_id:
            return ReceiptStatus.DELIVERED if result.delivered else ReceiptStatus.VALID
        return ReceiptStatus.INVALID

    @staticmethod
    def evaluate_return(
        request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        tx = context.transaction or {}
        version = tx.get("version", context.transaction_version)

        if request.action == Action.REGISTER_RETURN_WAYBILL:
            if request.actor_id != tx.get("buyer_id"):
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="구매자만 반품 운송장을 접수할 수 있습니다.",
                    expected_version=version,
                    status_code=403,
                )
            waybill_no = request.payload.get("waybill_no", "")
            previous = next(
                (
                    r
                    for r in tx.get("receipts", [])
                    if r["request_id"] == request.request_id
                ),
                None,
            )
            if previous is not None:
                if previous["waybill_no"] != waybill_no:
                    return PolicyDecision(
                        verdict=DecisionType.REJECT,
                        decision_id=_decision_id(),
                        reason="같은 요청 ID에 다른 운송장을 사용할 수 없습니다.",
                        expected_version=version,
                        status_code=409,
                    )
                return PolicyDecision(
                    verdict=DecisionType.APPROVE,
                    decision_id=_decision_id(),
                    reason="동일 요청 ID 재전송으로 기존 접수 기록을 반환합니다.",
                    actions=(Action.REPLAY_RETURN_WAYBILL,),
                    expected_version=version,
                    metadata={"existing_receipt_id": previous["id"]},
                )
            deadline = instant(tx["deadline"])
            if not DeadlinePolicy.is_before_deadline(request.received_at, deadline):
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="반품 운송장 등록 기한이 지났습니다.",
                    expected_version=version,
                    status_code=409,
                )
            if tx.get("operations"):
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="이미 지급 또는 환불이 결정된 거래입니다.",
                    expected_version=version,
                    status_code=409,
                )
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="기한 내 반품 운송장 접수를 승인합니다.",
                actions=(Action.ACCEPT_RETURN_WAYBILL,),
                expected_version=version,
            )

        if request.action == Action.EVALUATE_RETURN_SHIPMENT:
            result: ShipmentResult = request.payload["shipment_result"]
            seller_id: str = request.payload["seller_id"]
            current_attempts: int = request.payload["current_attempts"]
            max_attempts: int = request.payload["max_attempts"]
            status = ReturnPolicy.evaluate_shipment_result(
                result, seller_id, current_attempts, max_attempts
            )
            if status in (ReceiptStatus.VALID, ReceiptStatus.DELIVERED):
                verdict = DecisionType.APPROVE
                actions = (Action.HOLD_PAYOUT,)
                reason = "운송장과 판매자 수취인이 확인되었습니다."
            elif status == ReceiptStatus.INVALID:
                verdict = DecisionType.REJECT
                actions = ()
                reason = "운송장 무효 또는 수취인 불일치로 접수를 거절합니다."
            elif status == ReceiptStatus.MANUAL_REVIEW:
                verdict = DecisionType.HOLD
                actions = (Action.ESCALATE_MANUAL_REVIEW, Action.HOLD_PAYOUT)
                reason = "택배 조회 한도 도달로 담당자 확인으로 전환하고 지급 보류를 유지합니다."
            else:
                verdict = DecisionType.HOLD
                actions = (Action.HOLD_PAYOUT,)
                reason = "택배 응답 없음으로 재조회 대기 및 지급 보류를 유지합니다."
            return PolicyDecision(
                verdict=verdict,
                decision_id=_decision_id(),
                reason=reason,
                actions=actions,
                expected_version=version,
                metadata={
                    "receipt_status": status,
                    "attempts": current_attempts + 1,
                },
            )

        if request.action == Action.MARK_RETURN_ARRIVED:
            receipts = tx.get("receipts", [])
            if any(r["status"] == ReceiptStatus.DELIVERED for r in receipts):
                return PolicyDecision(
                    verdict=DecisionType.WAIT,
                    decision_id=_decision_id(),
                    reason="이미 반품 도착이 기록되어 있습니다.",
                    actions=(Action.WAIT,),
                    expected_version=version,
                )
            receipt = next(
                (r for r in receipts if r["status"] == ReceiptStatus.VALID),
                None,
            )
            if receipt is None:
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="유효성이 확인된 반품 운송장이 필요합니다.",
                    expected_version=version,
                    status_code=409,
                )
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="유효한 반품 운송장의 도착 기록을 승인합니다.",
                actions=(Action.MARK_RETURN_ARRIVED,),
                expected_version=version,
                metadata={"receipt_id": receipt["id"]},
            )

        # Default: Action.EVALUATE_RETURN_DEADLINE
        if tx.get("operations"):
            return PolicyDecision(
                verdict=DecisionType.WAIT,
                decision_id=_decision_id(),
                reason="이미 원금 지급 또는 환불 지시가 존재합니다.",
                actions=(Action.WAIT,),
                expected_version=version,
                metadata={"decision": Decision.WAIT},
            )
        deadline = instant(tx["deadline"])
        timely = [
            receipt
            for receipt in tx.get("receipts", [])
            if DeadlinePolicy.is_before_deadline(
                instant(receipt["received_at"]), deadline
            )
        ]
        if any(r["status"] == ReceiptStatus.DELIVERED for r in timely):
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="판매자에게 반품이 도착하여 구매자 환불을 승인합니다.",
                actions=(Action.START_REFUND,),
                expected_version=version,
                metadata={"decision": Decision.REFUND},
            )
        if any(r["status"] != ReceiptStatus.INVALID for r in timely):
            return PolicyDecision(
                verdict=DecisionType.HOLD,
                decision_id=_decision_id(),
                reason="기한 내 접수된 유효·확인 중·담당자 확인 운송장이 있어 지급 보류를 유지합니다.",
                actions=(Action.HOLD_PAYOUT,),
                expected_version=version,
                metadata={"decision": Decision.HOLD},
            )
        if DeadlinePolicy.is_due(context.now, deadline):
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="기한 만료 시점까지 유효·확인 중 운송장이 없어 판매자 지급을 승인합니다.",
                actions=(Action.START_PAYOUT,),
                expected_version=version,
                metadata={"decision": Decision.PAYOUT},
            )
        return PolicyDecision(
            verdict=DecisionType.WAIT,
            decision_id=_decision_id(),
            reason="반품 운송장 등록 마감 전입니다.",
            actions=(Action.WAIT,),
            expected_version=version,
            metadata={"decision": Decision.WAIT},
        )

    @staticmethod
    def evaluate(transaction: dict, now: datetime) -> Decision:
        decision = ReturnPolicy.evaluate_return(
            PolicyRequest(
                request_id="deadline-eval",
                action=Action.EVALUATE_RETURN_DEADLINE,
                actor_id=None,
                received_at=now,
            ),
            PolicyContext(
                now=now,
                transaction_version=transaction.get("version", 0),
                payment_held=True,
                transaction=transaction,
            ),
        )
        return decision.metadata["decision"]


class FundsPolicy:
    """Evaluates allocation-level financial operations and transfer result follow-ups."""

    @staticmethod
    def evaluate_operation(
        allocation: DepositAllocation,
        request: PolicyRequest,
        context: PolicyContext,
    ) -> PolicyDecision:
        tx = context.transaction or {}
        version = context.transaction_version

        if request.action == Action.HANDLE_TRANSFER_RESULT and context.attempt is not None:
            status = context.attempt.status
            if status == TransferStatus.SUCCEEDED:
                return PolicyDecision(
                    verdict=DecisionType.APPROVE,
                    decision_id=_decision_id(),
                    reason="은행 이체가 완료되어 금전 처리를 확정합니다.",
                    actions=(Action.COMPLETE_OPERATION,),
                    expected_version=version,
                )
            if status == TransferStatus.UNKNOWN:
                return PolicyDecision(
                    verdict=DecisionType.HOLD,
                    decision_id=_decision_id(),
                    reason="은행 응답이 불명확하므로 동일 requestKey로 결과를 재조회합니다.",
                    actions=(Action.RETRY_TRANSFER_LOOKUP,),
                    expected_version=version,
                )
            if status == TransferStatus.NOT_FOUND:
                idempotent_bank = bool(request.payload.get("bank_idempotent", True))
                if idempotent_bank:
                    return PolicyDecision(
                        verdict=DecisionType.HOLD,
                        decision_id=_decision_id(),
                        reason="은행 기록 미확인 상태이며 멱등 계약 하에 동일 requestKey 재전송을 검토합니다.",
                        actions=(Action.REPLAY_TRANSFER_SAME_KEY,),
                        expected_version=version,
                    )
                return PolicyDecision(
                    verdict=DecisionType.HOLD,
                    decision_id=_decision_id(),
                    reason="은행 멱등 계약이 확인되지 않아 새 키를 발급하지 않고 재조회를 유지합니다.",
                    actions=(Action.RETRY_TRANSFER_LOOKUP,),
                    expected_version=version,
                )
            # TransferStatus.FAILED
            return PolicyDecision(
                verdict=DecisionType.HOLD,
                decision_id=_decision_id(),
                reason="은행이 이체를 명시적으로 거절하여 담당자 확인 또는 계좌 정정 후 재시도가 필요합니다.",
                actions=(Action.ESCALATE_MANUAL_REVIEW,),
                expected_version=version,
            )

        if allocation.amount <= 0:
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="금전 처리 금액은 0보다 커야 합니다.",
                expected_version=version,
                status_code=409,
            )

        if allocation.kind == AllocationKind.EXCESS:
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="초과금 배분은 원금과 별도로 구매자 반환을 승인합니다.",
                actions=(Action.REFUND_EXCESS,),
                expected_version=version,
            )

        if allocation.kind == AllocationKind.UNAPPLIED:
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="미배분 입금은 거래 상태와 무관하게 구매자 반환을 승인합니다.",
                actions=(Action.REFUND_UNAPPLIED,),
                expected_version=version,
            )

        # AllocationKind.PRINCIPAL
        if allocation.operation_id is not None or tx.get("operations"):
            return PolicyDecision(
                verdict=DecisionType.REJECT,
                decision_id=_decision_id(),
                reason="동일 원금 배분에 대해 지급과 환불을 중복 승인할 수 없습니다.",
                expected_version=version,
                status_code=409,
            )
        op_type = request.payload.get("operation_type")
        if op_type == OperationType.PAYOUT:
            if context.payment_held:
                return PolicyDecision(
                    verdict=DecisionType.HOLD,
                    decision_id=_decision_id(),
                    reason="지급 보류 상태에서는 판매자 지급을 승인할 수 없습니다.",
                    actions=(Action.HOLD_PAYOUT,),
                    expected_version=version,
                    status_code=409,
                )
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="원금 판매자 지급을 승인합니다.",
                actions=(Action.START_PAYOUT,),
                expected_version=version,
            )
        if op_type == OperationType.REFUND:
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="원금 구매자 환불을 승인합니다.",
                actions=(Action.START_REFUND,),
                expected_version=version,
            )
        return PolicyDecision(
            verdict=DecisionType.REJECT,
            decision_id=_decision_id(),
            reason="지원하지 않는 금전 처리 유형입니다.",
            expected_version=version,
            status_code=400,
        )


class PolicyController:
    """Implements TransactionPolicy and delegates all validations to evaluate()."""

    def validate_join(
        self,
        transaction: dict,
        buyer_id: str,
        token: str,
        now: datetime | None = None,
    ) -> PolicyDecision:
        at = now or utc_now()
        return self.evaluate(
            PolicyRequest(
                request_id=f"join-{transaction.get('id', '')}",
                action=Action.JOIN_TRANSACTION,
                actor_id=buyer_id,
                received_at=at,
                payload={"token": token},
            ),
            PolicyContext(
                now=at,
                transaction_version=transaction.get("version", 0),
                transaction=transaction,
            ),
        )

    def validate_waybill(
        self,
        transaction: dict,
        seller_id: str,
        now: datetime | None = None,
    ) -> PolicyDecision:
        at = now or utc_now()
        return self.evaluate(
            PolicyRequest(
                request_id=f"waybill-{transaction.get('id', '')}",
                action=Action.REGISTER_WAYBILL,
                actor_id=seller_id,
                received_at=at,
            ),
            PolicyContext(
                now=at,
                transaction_version=transaction.get("version", 0),
                waybill_registered=bool(transaction.get("waybill_no")),
                transaction=transaction,
            ),
        )

    def validate_purchase(
        self,
        transaction: dict,
        buyer_id: str,
        now: datetime | None = None,
    ) -> PolicyDecision:
        at = now or utc_now()
        return self.evaluate(
            PolicyRequest(
                request_id=f"purchase-{transaction.get('id', '')}",
                action=Action.CONFIRM_PURCHASE,
                actor_id=buyer_id,
                received_at=at,
            ),
            PolicyContext(
                now=at,
                transaction_version=transaction.get("version", 0),
                waybill_registered=bool(transaction.get("waybill_no")),
                payment_held=bool(transaction.get("payment_held", False)),
                transaction=transaction,
            ),
        )

    def validate_cancellation(
        self,
        transaction: dict,
        actor_id: str,
        now: datetime | None = None,
    ) -> PolicyDecision:
        at = now or utc_now()
        return self.evaluate(
            PolicyRequest(
                request_id=f"cancel-{transaction.get('id', '')}",
                action=Action.CANCEL_TRANSACTION,
                actor_id=actor_id,
                received_at=at,
            ),
            PolicyContext(
                now=at,
                transaction_version=transaction.get("version", 0),
                waybill_registered=bool(transaction.get("waybill_no")),
                transaction=transaction,
            ),
        )

    def validate_read(
        self,
        transaction: dict,
        actor_id: str,
        now: datetime | None = None,
    ) -> PolicyDecision:
        at = now or utc_now()
        return self.evaluate(
            PolicyRequest(
                request_id=f"read-{transaction.get('id', '')}",
                action=Action.READ_TRANSACTION,
                actor_id=actor_id,
                received_at=at,
            ),
            PolicyContext(
                now=at,
                transaction_version=transaction.get("version", 0),
                transaction=transaction,
            ),
        )

    def evaluate(
        self, request: PolicyRequest, context: PolicyContext
    ) -> PolicyDecision:
        tx = context.transaction or {}
        version = tx.get("version", context.transaction_version)

        if request.action == Action.JOIN_TRANSACTION:
            if tx.get("status") != TransactionStatus.WAITING_BUYER:
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="구매자 참여 대기 상태의 거래만 참여할 수 있습니다.",
                    expected_version=version,
                    status_code=409,
                )
            if request.actor_id == tx.get("seller_id"):
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="판매자는 본인 거래에 구매자로 참여할 수 없습니다.",
                    expected_version=version,
                    status_code=403,
                )
            if request.payload.get("token") != tx.get("invite_token"):
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="초대 토큰이 일치하지 않습니다.",
                    expected_version=version,
                    status_code=403,
                )
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="거래 참여가 승인되었습니다.",
                actions=(Action.JOIN_TRANSACTION,),
                expected_version=version,
            )

        if request.action == Action.READ_TRANSACTION:
            if request.actor_id not in (tx.get("buyer_id"), tx.get("seller_id")):
                return PolicyDecision(
                    verdict=DecisionType.REJECT,
                    decision_id=_decision_id(),
                    reason="거래 참여자만 조회할 수 있습니다.",
                    expected_version=version,
                    status_code=403,
                )
            return PolicyDecision(
                verdict=DecisionType.APPROVE,
                decision_id=_decision_id(),
                reason="거래 조회가 승인되었습니다.",
                actions=(Action.READ_TRANSACTION,),
                expected_version=version,
            )

        if request.action == Action.CONFIRM_DEPOSIT and context.deposit is not None:
            return DepositPolicy.evaluate_deposit(context.deposit, context)

        if request.action == Action.REGISTER_WAYBILL:
            return FulfillmentPolicy.evaluate_shipment(request, context)

        if request.action in (Action.CONFIRM_PURCHASE, Action.AUTO_CONFIRM_PURCHASE):
            return FulfillmentPolicy.evaluate_confirmation(request, context)

        if request.action in (
            Action.CANCEL_TRANSACTION,
            Action.AUTO_CANCEL_TRANSACTION,
        ):
            return CancellationPolicy.evaluate_cancellation(request, context)

        if request.action == Action.RAISE_ISSUE:
            return ReturnPolicy.evaluate_issue(request, context)

        if request.action in (
            Action.REGISTER_RETURN_WAYBILL,
            Action.EVALUATE_RETURN_SHIPMENT,
            Action.MARK_RETURN_ARRIVED,
            Action.EVALUATE_RETURN_DEADLINE,
        ):
            return ReturnPolicy.evaluate_return(request, context)

        if (
            request.action
            in (Action.EXECUTE_FUNDS_OPERATION, Action.HANDLE_TRANSFER_RESULT)
            and context.allocation is not None
        ):
            return FundsPolicy.evaluate_operation(context.allocation, request, context)

        return PolicyDecision(
            verdict=DecisionType.REJECT,
            decision_id=_decision_id(),
            reason=f"지원하지 않는 정책 요청입니다: {request.action}",
            expected_version=version,
            status_code=400,
        )

    def evaluate_return_deadline(
        self, transaction: dict, now: datetime
    ) -> PolicyDecision:
        notice_at = transaction.get("return_notice_published_at")
        return self.evaluate(
            PolicyRequest(
                request_id=f"deadline-{transaction.get('id', '')}",
                action=Action.EVALUATE_RETURN_DEADLINE,
                actor_id=None,
                received_at=now,
            ),
            PolicyContext(
                now=now,
                transaction_version=transaction.get("version", 0),
                payment_held=True,
                return_notice_published_at=(
                    instant(notice_at) if isinstance(notice_at, str) else notice_at
                ),
                transaction=transaction,
            ),
        )
