"""거래와 상태 (통합 DCD의 Transaction, TransactionStatus)."""
from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class TransactionStatus(str, Enum):
    WAITING_BUYER = "WAITING_BUYER"
    WAITING_DEPOSIT = "WAITING_DEPOSIT"
    DEPOSIT_CONFIRMED = "DEPOSIT_CONFIRMED"
    WAYBILL_REGISTERED = "WAYBILL_REGISTERED"
    PAYOUT_PROCESSING = "PAYOUT_PROCESSING"
    COMPLETED = "COMPLETED"
    REFUND_PROCESSING = "REFUND_PROCESSING"
    CANCELLED = "CANCELLED"


class InvalidTransition(Exception):
    pass


@dataclass
class Transaction:
    id: str
    seller_id: str
    amount: int
    invite_token: str
    buyer_id: Optional[str] = None
    waybill_no: Optional[str] = None
    status: TransactionStatus = TransactionStatus.WAITING_BUYER
    version: int = 0  # 낙관적 락용 (저장할 때마다 1 증가)

    @classmethod
    def create(cls, seller_id: str, amount: int) -> "Transaction":
        return cls(id=str(uuid.uuid4()), seller_id=seller_id, amount=amount, invite_token=secrets.token_urlsafe(8))

    # 지급·환불에 쓰는 멱등키. 같은 거래의 같은 이체는 항상 같은 키 → 은행에서 1건만 처리
    @property
    def payout_request_key(self) -> str:
        return f"PAYOUT-{self.id}"

    @property
    def refund_request_key(self) -> str:
        return f"REFUND-{self.id}"

    def _move(self, allowed: Iterable[TransactionStatus], to: TransactionStatus) -> None:
        if self.status not in allowed:
            raise InvalidTransition(f"{self.status.value} -> {to.value} 불가")
        self.status = to

    def attach_buyer(self, buyer_id: str) -> None:
        self._move([TransactionStatus.WAITING_BUYER], TransactionStatus.WAITING_DEPOSIT)
        self.buyer_id = buyer_id

    def mark_deposit_confirmed(self) -> None:
        self._move([TransactionStatus.WAITING_DEPOSIT], TransactionStatus.DEPOSIT_CONFIRMED)

    def mark_waybill_registered(self, waybill_no: str) -> None:
        self._move([TransactionStatus.DEPOSIT_CONFIRMED], TransactionStatus.WAYBILL_REGISTERED)
        self.waybill_no = waybill_no

    def start_payout(self) -> None:
        self._move([TransactionStatus.WAYBILL_REGISTERED], TransactionStatus.PAYOUT_PROCESSING)

    def complete_payout(self) -> None:
        self._move([TransactionStatus.PAYOUT_PROCESSING], TransactionStatus.COMPLETED)

    def cancel_before_deposit(self) -> None:
        self._move([TransactionStatus.WAITING_BUYER, TransactionStatus.WAITING_DEPOSIT], TransactionStatus.CANCELLED)

    def start_refund(self) -> None:
        self._move([TransactionStatus.DEPOSIT_CONFIRMED], TransactionStatus.REFUND_PROCESSING)

    def complete_cancellation(self) -> None:
        self._move([TransactionStatus.REFUND_PROCESSING], TransactionStatus.CANCELLED)
