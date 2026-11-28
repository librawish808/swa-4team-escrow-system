"""BankPort 구현 (통합 DCD의 BankAdapter).

가용성·장애 복구성 QAS의 Tactic이 들어 있는 곳:
- 예외 감지·변환: 외부 예외를 B로 던지지 않고 UNKNOWN 결과로 바꾼다.
- Retry(조회 전용): 입금·결과 조회는 부작용이 없으므로 lookup_attempts번까지 재시도한다.
- 지급·환불 요청은 재시도하지 않는다. 은행이 이미 처리했을 수 있으므로 UNKNOWN을 돌려준다.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from .bank_client import BankApiClient
from .bank_mapper import BankResponseMapper
from .errors import ExternalCallError
from .ports import BankPort
from .results import DepositCheckResult, LookupStatus, TransferResult, TransferStatus

log = logging.getLogger(__name__)


class BankAdapter(BankPort):
    def __init__(
        self,
        client: BankApiClient,
        mapper: Optional[BankResponseMapper] = None,
        lookup_attempts: int = 2,
    ) -> None:
        self.client = client
        self.mapper = mapper or BankResponseMapper()
        self.lookup_attempts = lookup_attempts

    # ---------- 조회: 재시도 가능 ----------
    def check_deposit(self, transaction_id: str) -> DepositCheckResult:
        response = self._lookup(lambda: self.client.query_deposit(transaction_id))
        if response is None:
            return DepositCheckResult(status=LookupStatus.UNKNOWN, message="bank unavailable")
        return self.mapper.to_deposit_result(response)

    def get_transfer_result(self, request_key: str) -> TransferResult:
        response = self._lookup(lambda: self.client.query_transfer(request_key))
        if response is None:
            return TransferResult(request_key, TransferStatus.UNKNOWN, message="bank unavailable")
        return self.mapper.to_transfer_result(response)

    # ---------- 이체 요청: 재시도하지 않음 ----------
    def request_payout(self, transaction_id: str, request_key: str) -> TransferResult:
        return self._submit(lambda: self.client.submit_payout(transaction_id, request_key), request_key)

    def request_refund(self, transaction_id: str, request_key: str) -> TransferResult:
        return self._submit(lambda: self.client.submit_refund(transaction_id, request_key), request_key)

    # ---------- 내부 ----------
    def _submit(self, call: Callable[[], dict], request_key: str) -> TransferResult:
        try:
            return self.mapper.to_transfer_result(call())
        except ExternalCallError as e:
            log.warning("transfer %s: %s -> UNKNOWN", request_key, e)
            return TransferResult(request_key, TransferStatus.UNKNOWN, message=str(e))

    def _lookup(self, call: Callable[[], dict]) -> Optional[dict]:
        for attempt in range(1, self.lookup_attempts + 1):
            try:
                return call()
            except ExternalCallError as e:
                log.warning("bank lookup attempt %d/%d failed: %s", attempt, self.lookup_attempts, e)
        return None
