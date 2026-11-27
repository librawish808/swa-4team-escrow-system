"""거래 저장소 (통합 DCD의 TransactionRepository).

SQLite 파일에 저장하므로 서버(프로세스)가 죽었다가 다시 떠도 거래 상태가 남는다.
이것이 내부 장애 복구(QAS-AV-02)의 전제다.
"""
from __future__ import annotations

import sqlite3
import threading
from abc import ABC, abstractmethod
from typing import Iterable, List, Optional

from .models import Transaction, TransactionStatus


class ConcurrentModificationError(Exception):
    """읽은 뒤 다른 요청이 먼저 저장함 (낙관적 락 충돌)."""


class TransactionRepository(ABC):
    @abstractmethod
    def find_by_id(self, transaction_id: str) -> Optional[Transaction]: ...

    @abstractmethod
    def find_by_status(self, statuses: Iterable[TransactionStatus]) -> List[Transaction]: ...

    @abstractmethod
    def save(self, transaction: Transaction) -> None: ...

    def find_all(self) -> List[Transaction]:
        """데모 화면용 전체 목록 (최근 생성 순)."""
        return []


class SqliteTransactionRepository(TransactionRepository):
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, buyer_id TEXT,
                amount INTEGER NOT NULL, invite_token TEXT NOT NULL, waybill_no TEXT,
                status TEXT NOT NULL, version INTEGER NOT NULL)"""
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row_to_tx(row) -> Transaction:
        return Transaction(
            id=row[0], seller_id=row[1], buyer_id=row[2], amount=row[3], invite_token=row[4],
            waybill_no=row[5], status=TransactionStatus(row[6]), version=row[7],
        )

    _COLUMNS = "id, seller_id, buyer_id, amount, invite_token, waybill_no, status, version"

    def find_by_id(self, transaction_id: str) -> Optional[Transaction]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM transactions WHERE id = ?", (transaction_id,)
            ).fetchone()
        return self._row_to_tx(row) if row else None

    def find_by_status(self, statuses: Iterable[TransactionStatus]) -> List[Transaction]:
        values = [s.value for s in statuses]
        marks = ",".join("?" * len(values))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM transactions WHERE status IN ({marks})", values
            ).fetchall()
        return [self._row_to_tx(r) for r in rows]

    def find_all(self) -> List[Transaction]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM transactions ORDER BY rowid DESC"
            ).fetchall()
        return [self._row_to_tx(r) for r in rows]

    def clear(self) -> None:
        """데모 초기화용."""
        with self._lock:
            self._conn.execute("DELETE FROM transactions")
            self._conn.commit()

    def save(self, tx: Transaction) -> None:
        with self._lock:
            cur = self._conn.execute(
                """UPDATE transactions SET buyer_id=?, waybill_no=?, status=?, version=version+1
                   WHERE id=? AND version=?""",
                (tx.buyer_id, tx.waybill_no, tx.status.value, tx.id, tx.version),
            )
            if cur.rowcount == 1:
                self._conn.commit()
                tx.version += 1
                return
            exists = self._conn.execute("SELECT 1 FROM transactions WHERE id=?", (tx.id,)).fetchone()
            if exists:
                self._conn.rollback()
                raise ConcurrentModificationError(f"transaction {tx.id} was modified concurrently")
            self._conn.execute(
                f"INSERT INTO transactions ({self._COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
                (tx.id, tx.seller_id, tx.buyer_id, tx.amount, tx.invite_token, tx.waybill_no, tx.status.value, tx.version),
            )
            self._conn.commit()
