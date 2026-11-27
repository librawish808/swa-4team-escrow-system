"""외부 시스템 호출 결과 타입 (통합 DCD의 TransferResult, DepositCheckResult, ShipmentResult)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class TransferStatus(str, Enum):
    """지급·환불 요청 결과."""

    SUCCEEDED = "SUCCEEDED"  # 은행이 이체를 완료함
    FAILED = "FAILED"  # 은행이 명시적으로 거절함 (계좌 오류 등)
    UNKNOWN = "UNKNOWN"  # 시간 초과·응답 유실로 결과를 알 수 없음 (FAILED 아님)
    NOT_FOUND = "NOT_FOUND"  # 은행에 해당 requestKey 기록이 없음 (요청이 도달하지 않음)


class LookupStatus(str, Enum):
    """입금·배송 조회 결과."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"  # 외부 장애로 조회하지 못함


@dataclass(frozen=True)
class TransferResult:
    request_key: str
    status: TransferStatus
    bank_transaction_id: Optional[str] = None
    message: str = ""


@dataclass(frozen=True)
class DepositCheckResult:
    status: LookupStatus
    bank_deposit_id: Optional[str] = None
    received_amount: Optional[int] = None
    bank_deposited_at: Optional[datetime] = None
    message: str = ""


@dataclass(frozen=True)
class ShipmentResult:
    status: LookupStatus
    waybill_no: Optional[str] = None
    recipient: Optional[str] = None
    delivered_at: Optional[datetime] = None
    message: str = ""
