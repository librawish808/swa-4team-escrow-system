"""QAS-01 domain and pure policy decisions (DCD: C)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol


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


class ReceiptStatus(StrEnum):
    PENDING = "PENDING"
    VALID = "VALID"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    DELIVERED = "DELIVERED"


@dataclass(frozen=True)
class ShipmentResult:
    valid: bool | None
    recipient_id: str | None = None
    delivered: bool = False


class DeliveryPort(Protocol):
    async def get_shipment_status(
        self, waybill_no: str, seller_id: str
    ) -> ShipmentResult: ...


class BankPort(Protocol):
    async def transfer(self, operation: dict) -> str: ...


class DeadlinePolicy:
    @staticmethod
    def deadline_of(notice_published_at: datetime) -> datetime:
        return notice_published_at + timedelta(hours=72)

    @staticmethod
    def is_before_deadline(received_at: datetime, deadline: datetime) -> bool:
        return received_at < deadline


class Decision(StrEnum):
    WAIT = "WAIT"
    HOLD = "HOLD"
    PAYOUT = "PAYOUT"
    REFUND = "REFUND"


class ReturnPolicy:
    @staticmethod
    def evaluate(transaction: dict, now: datetime) -> Decision:
        if transaction["operations"]:
            return Decision.WAIT
        timely = [
            receipt
            for receipt in transaction["receipts"]
            if DeadlinePolicy.is_before_deadline(
                instant(receipt["received_at"]), instant(transaction["deadline"])
            )
        ]
        if any(r["status"] == ReceiptStatus.DELIVERED for r in timely):
            return Decision.REFUND
        if any(r["status"] != ReceiptStatus.INVALID for r in timely):
            return Decision.HOLD
        if now >= instant(transaction["deadline"]):
            return Decision.PAYOUT
        return Decision.WAIT


class PolicyController:
    """C decides; B and the repository perform side effects."""

    def evaluate(self, transaction: dict, now: datetime) -> Decision:
        return ReturnPolicy.evaluate(transaction, now)
