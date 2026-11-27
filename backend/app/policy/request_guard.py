"""중복 지급·환불 방지 (통합 DCD의 RequestGuard).

점유 기록을 SQLite에 남기므로 서버가 재시작돼도 같은 이체를 두 번 시작하지 않는다.
"""
from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod


class RequestGuard(ABC):
    @abstractmethod
    def claim_once(self, transaction_id: str, operation_type: str) -> bool:
        """처음 요청이면 True, 이미 점유된 요청이면 False."""


class SqliteRequestGuard(RequestGuard):
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS operation_claims (
                transaction_id TEXT NOT NULL, operation_type TEXT NOT NULL,
                PRIMARY KEY (transaction_id, operation_type))"""
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def clear(self) -> None:
        """데모 초기화용."""
        with self._lock:
            self._conn.execute("DELETE FROM operation_claims")
            self._conn.commit()

    def claim_once(self, transaction_id: str, operation_type: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO operation_claims VALUES (?, ?)", (transaction_id, operation_type)
            )
            self._conn.commit()
            return cur.rowcount == 1
