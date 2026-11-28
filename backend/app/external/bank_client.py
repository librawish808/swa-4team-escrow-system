"""은행 API 호출 담당 (통합 DCD의 BankApiClient). 타임아웃(장애 감지)을 여기서 처리한다."""
from __future__ import annotations

import time
from typing import Callable

from .errors import ExternalTimeoutError
from .mock_bank import MockBankServer


class BankApiClient:
    def __init__(self, server: MockBankServer, timeout_millis: int = 3000) -> None:
        self.server = server
        self.timeout_millis = timeout_millis

    def _call(self, fn: Callable[[], dict]) -> dict:
        response = fn()  # 연결 실패·5xx면 여기서 예외
        if self.server.response_lost:
            # 은행은 처리했지만 응답이 timeout_millis 안에 오지 않음 → 기다리다 포기
            time.sleep(self.timeout_millis / 1000)
            raise ExternalTimeoutError(f"bank: no response within {self.timeout_millis}ms")
        return response

    def query_deposit(self, transaction_id: str) -> dict:
        return self._call(lambda: self.server.query_deposit(transaction_id))

    def submit_payout(self, transaction_id: str, request_key: str) -> dict:
        return self._call(lambda: self.server.submit_transfer("PAYOUT", transaction_id, request_key))

    def submit_refund(self, transaction_id: str, request_key: str) -> dict:
        return self._call(lambda: self.server.submit_transfer("REFUND", transaction_id, request_key))

    def query_transfer(self, request_key: str) -> dict:
        return self._call(lambda: self.server.query_transfer(request_key))
