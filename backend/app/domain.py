"""Escrow domain models, funds records, external port contracts, and policy DTOs."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class DomainError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def utc_now() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timezone-aware timestamps are required")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


class TransactionStatus(StrEnum):
    """Integrated DCD TransactionStatus."""

    WAITING_BUYER = "WAITING_BUYER"
    WAITING_DEPOSIT = "WAITING_DEPOSIT"
    DEPOSIT_CONFIRMED = "DEPOSIT_CONFIRMED"
    WAYBILL_REGISTERED = "WAYBILL_REGISTERED"
    PAYOUT_PROCESSING = "PAYOUT_PROCESSING"
    COMPLETED = "COMPLETED"
    REFUND_PROCESSING = "REFUND_PROCESSING"
    CANCELLED = "CANCELLED"


class ReceiptStatus(StrEnum):
    PENDING = "PENDING"
    VALID = "VALID"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    DELIVERED = "DELIVERED"


class AllocationKind(StrEnum):
    PRINCIPAL = "PRINCIPAL"
    EXCESS = "EXCESS"
    UNAPPLIED = "UNAPPLIED"


class OperationType(StrEnum):
    PAYOUT = "PAYOUT"
    REFUND = "REFUND"


class OperationStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class TransferStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"


class LookupStatus(StrEnum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Deposit:
    bank_deposit_id: str
    received_amount: int
    bank_deposited_at: datetime


@dataclass(frozen=True)
class DepositAllocation:
    kind: AllocationKind
    amount: int
    bank_deposit_id: str = ""
    operation_id: str | None = None


@dataclass(frozen=True)
class FundsOperation:
    operation_id: str
    type: OperationType
    recipient_id: str
    amount: int
    status: OperationStatus = OperationStatus.PENDING
    allocation_kind: AllocationKind = AllocationKind.PRINCIPAL


@dataclass(frozen=True)
class TransferAttempt:
    request_key: str
    recipient_account: str
    status: TransferStatus
    bank_transaction_id: str | None = None


@dataclass(frozen=True)
class DepositCheckResult:
    status: LookupStatus
    bank_deposit_id: str | None = None
    received_amount: int | None = None
    bank_deposited_at: datetime | None = None


@dataclass(frozen=True)
class TransferResult:
    request_key: str
    status: TransferStatus
    bank_transaction_id: str | None = None


@dataclass(frozen=True)
class ShipmentResult:
    valid: bool | None
    recipient_id: str | None = None
    delivered: bool = False
    status: LookupStatus | None = None
    waybill_no: str | None = None
    delivered_at: datetime | None = None

    def __post_init__(self):
        if self.status is None:
            inferred = (
                LookupStatus.UNKNOWN
                if self.valid is None
                else (LookupStatus.FOUND if self.valid else LookupStatus.NOT_FOUND)
            )
            object.__setattr__(self, "status", inferred)


class DeliveryPort(Protocol):
    async def register_waybill(
        self, transaction_id: str, waybill_no: str
    ) -> ShipmentResult: ...

    async def get_shipment_status(
        self, waybill_no: str, seller_id: str
    ) -> ShipmentResult: ...


class BankPort(Protocol):
    async def check_deposit(self, transaction_id: str) -> DepositCheckResult: ...

    async def request_payout(
        self, transaction_id: str, request_key: str, operation: dict | None = None
    ) -> TransferResult: ...

    async def request_refund(
        self, transaction_id: str, request_key: str, operation: dict | None = None
    ) -> TransferResult: ...

    async def get_transfer_result(self, request_key: str) -> TransferResult: ...


class Action(StrEnum):
    JOIN_TRANSACTION = "JOIN_TRANSACTION"
    CONFIRM_DEPOSIT = "CONFIRM_DEPOSIT"
    REGISTER_WAYBILL = "REGISTER_WAYBILL"
    CONFIRM_PURCHASE = "CONFIRM_PURCHASE"
    AUTO_CONFIRM_PURCHASE = "AUTO_CONFIRM_PURCHASE"
    CANCEL_TRANSACTION = "CANCEL_TRANSACTION"
    AUTO_CANCEL_TRANSACTION = "AUTO_CANCEL_TRANSACTION"
    READ_TRANSACTION = "READ_TRANSACTION"
    RAISE_ISSUE = "RAISE_ISSUE"
    REGISTER_RETURN_WAYBILL = "REGISTER_RETURN_WAYBILL"
    EVALUATE_RETURN_SHIPMENT = "EVALUATE_RETURN_SHIPMENT"
    MARK_RETURN_ARRIVED = "MARK_RETURN_ARRIVED"
    EVALUATE_RETURN_DEADLINE = "EVALUATE_RETURN_DEADLINE"
    EXECUTE_FUNDS_OPERATION = "EXECUTE_FUNDS_OPERATION"
    HANDLE_TRANSFER_RESULT = "HANDLE_TRANSFER_RESULT"
    ACCEPT_RETURN_WAYBILL = "ACCEPT_RETURN_WAYBILL"
    REPLAY_RETURN_WAYBILL = "REPLAY_RETURN_WAYBILL"
    HOLD_PAYOUT = "HOLD_PAYOUT"
    START_PAYOUT = "START_PAYOUT"
    START_REFUND = "START_REFUND"
    REFUND_EXCESS = "REFUND_EXCESS"
    REFUND_UNAPPLIED = "REFUND_UNAPPLIED"
    COMPLETE_OPERATION = "COMPLETE_OPERATION"
    RETRY_TRANSFER_LOOKUP = "RETRY_TRANSFER_LOOKUP"
    REPLAY_TRANSFER_SAME_KEY = "REPLAY_TRANSFER_SAME_KEY"
    ESCALATE_MANUAL_REVIEW = "ESCALATE_MANUAL_REVIEW"
    WAIT = "WAIT"


class DecisionType(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    HOLD = "HOLD"
    WAIT = "WAIT"


class Decision(StrEnum):
    """QAS-01 return deadline decision enum."""

    WAIT = "WAIT"
    HOLD = "HOLD"
    PAYOUT = "PAYOUT"
    REFUND = "REFUND"


@dataclass(frozen=True)
class PolicyRequest:
    request_id: str
    action: Action
    actor_id: str | None
    received_at: datetime
    sequence: int = 0
    receipt_confirmed: bool = False
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyContext:
    now: datetime
    transaction_version: int = 0
    funds_version: int = 0
    policy_version: int = 1
    waybill_registered: bool = False
    payment_held: bool = False
    delivered_at: datetime | None = None
    return_notice_published_at: datetime | None = None
    transaction: dict[str, Any] | None = None
    deposit: Deposit | None = None
    allocation: DepositAllocation | None = None
    attempt: TransferAttempt | None = None


@dataclass(frozen=True)
class PolicyDecision:
    verdict: DecisionType
    decision_id: str
    reason: str
    actions: tuple[Action, ...] = ()
    expected_version: int = 0
    status_code: int = 200
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.verdict == DecisionType.APPROVE

