"""외부 은행 시뮬레이터 (데모·테스트용).

실제 은행 대신 이 객체가 '외부 시스템' 역할을 한다. 장애를 주입할 수 있다.

가정 (보고서에 명시):
- 은행은 requestKey로 중복 이체를 막는다 (같은 키로 두 번 와도 1건만 처리).
- 우리 거래 ID로 입금·이체 대상을 찾을 수 있다 (실제 연동 시 계좌·원입금 ID 매핑 필요).
"""
from __future__ import annotations

import itertools
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from .errors import ExternalConnectionError, ExternalServerError


class BankFault(str, Enum):
    NONE = "NONE"  # 정상
    DOWN = "DOWN"  # 연결 실패. 요청이 처리되지 않음
    SERVER_ERROR = "SERVER_ERROR"  # 5xx. 요청이 처리되지 않음
    RESPONSE_LOST = "RESPONSE_LOST"  # 요청은 처리되지만 응답이 타임아웃 뒤에 옴


class MockBankServer:
    def __init__(self) -> None:
        self.fault = BankFault.NONE
        self._transient_failures = 0
        self._deposits: Dict[str, dict] = {}
        self._transfers: Dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.call_count = 0
        self.submit_attempts: Dict[str, int] = {}  # requestKey별 이체 요청 도달 횟수

    # ---------- 장애 주입 ----------
    def set_fault(self, fault: BankFault) -> None:
        self.fault = fault

    def fail_next(self, times: int) -> None:
        """다음 times번의 호출만 연결 실패시킨다 (일시 장애)."""
        self._transient_failures = times

    @property
    def response_lost(self) -> bool:
        return self.fault == BankFault.RESPONSE_LOST

    def _check_reachable(self) -> None:
        self.call_count += 1
        if self._transient_failures > 0:
            self._transient_failures -= 1
            raise ExternalConnectionError("bank: connection refused (transient)")
        if self.fault == BankFault.DOWN:
            raise ExternalConnectionError("bank: connection refused")
        if self.fault == BankFault.SERVER_ERROR:
            raise ExternalServerError("bank: 503 Service Unavailable")

    # ---------- 테스트 준비용 (구매자가 은행에 입금) ----------
    def make_deposit(self, transaction_id: str, amount: int) -> None:
        self._deposits[transaction_id] = {
            "depositId": f"DEP-{next(self._ids)}",
            "amount": amount,
            "depositedAt": datetime.now(timezone.utc).isoformat(),
        }

    # ---------- 은행 API ----------
    def query_deposit(self, transaction_id: str) -> dict:
        self._check_reachable()
        dep = self._deposits.get(transaction_id)
        if dep is None:
            return {"found": False}
        return {"found": True, **dep}

    def submit_transfer(self, kind: str, transaction_id: str, request_key: str) -> dict:
        self._check_reachable()
        with self._lock:
            self.submit_attempts[request_key] = self.submit_attempts.get(request_key, 0) + 1
            existing = self._transfers.get(request_key)
            if existing is not None:
                # 같은 requestKey 재요청: 새로 이체하지 않고 기존 결과를 돌려준다 (멱등)
                return {"found": True, **existing, "duplicate": True}
            dep = self._deposits.get(transaction_id)
            status = "DONE" if dep is not None else "REJECTED"
            record = {
                "requestKey": request_key,
                "kind": kind,
                "transactionId": transaction_id,
                "amount": dep["amount"] if dep else 0,
                "status": status,
                "bankTxId": f"BTX-{next(self._ids)}",
            }
            self._transfers[request_key] = record
            return {"found": True, **record, "duplicate": False}

    def query_transfer(self, request_key: str) -> dict:
        self._check_reachable()
        rec = self._transfers.get(request_key)
        if rec is None:
            return {"found": False, "requestKey": request_key}
        return {"found": True, **rec}

    # ---------- 검증용 ----------
    def all_transfers(self) -> List[dict]:
        return [dict(r, attempts=self.submit_attempts.get(k, 0)) for k, r in self._transfers.items()]

    def executed_transfers(self, transaction_id: Optional[str] = None, kind: Optional[str] = None) -> List[dict]:
        return [
            r
            for r in self._transfers.values()
            if r["status"] == "DONE"
            and (transaction_id is None or r["transactionId"] == transaction_id)
            and (kind is None or r["kind"] == kind)
        ]
