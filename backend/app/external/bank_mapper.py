"""은행 응답 → 내부 결과 타입 변환 (통합 DCD의 BankResponseMapper)."""
from __future__ import annotations

from datetime import datetime

from .results import DepositCheckResult, LookupStatus, TransferResult, TransferStatus


class BankResponseMapper:
    def to_deposit_result(self, response: dict) -> DepositCheckResult:
        if not response.get("found"):
            return DepositCheckResult(status=LookupStatus.NOT_FOUND)
        return DepositCheckResult(
            status=LookupStatus.FOUND,
            bank_deposit_id=response["depositId"],
            received_amount=response["amount"],
            bank_deposited_at=datetime.fromisoformat(response["depositedAt"]),
        )

    def to_transfer_result(self, response: dict) -> TransferResult:
        key = response.get("requestKey", "")
        if not response.get("found"):
            return TransferResult(request_key=key, status=TransferStatus.NOT_FOUND)
        status = TransferStatus.SUCCEEDED if response["status"] == "DONE" else TransferStatus.FAILED
        return TransferResult(request_key=key, status=status, bank_transaction_id=response.get("bankTxId"))
