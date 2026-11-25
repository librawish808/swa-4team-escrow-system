"""SQLite persistence and atomic version/financial guard (DCD: infrastructure)."""

import sqlite3
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .domain import (
    DeadlinePolicy,
    Decision,
    DomainError,
    ReceiptStatus,
    instant,
    timestamp,
)


class SqlTransactionRepository:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
                    amount INTEGER NOT NULL CHECK(amount > 0),
                    return_notice_published_at TEXT NOT NULL, deadline TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0, hold_decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id TEXT PRIMARY KEY, transaction_id TEXT NOT NULL REFERENCES transactions(id),
                    request_id TEXT NOT NULL, waybill_no TEXT NOT NULL, received_at TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_check_at TEXT NOT NULL, lease_token TEXT, lease_until TEXT,
                    UNIQUE(transaction_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS receipts_due ON receipts(status, next_check_at);
                CREATE TABLE IF NOT EXISTS operations (
                    request_key TEXT PRIMARY KEY,
                    transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(id),
                    kind TEXT NOT NULL CHECK(kind IN ('PAYOUT', 'REFUND')),
                    recipient_id TEXT NOT NULL, amount INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                );
                CREATE TABLE IF NOT EXISTS mock_bank_transfers (
                    request_key TEXT PRIMARY KEY, transaction_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, recipient_id TEXT NOT NULL, amount INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    transaction_id TEXT NOT NULL REFERENCES transactions(id),
                    at TEXT NOT NULL, message TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def event(db, transaction_id, now, message):
        db.execute(
            "INSERT INTO events(transaction_id, at, message) VALUES (?, ?, ?)",
            (transaction_id, timestamp(now), message),
        )

    def create_approved_return(self, notice_published_at, amount=100_000):
        transaction_id = str(uuid4())
        with self.connection(write=True) as db:
            db.execute(
                """INSERT INTO transactions
                (id, seller_id, buyer_id, amount, return_notice_published_at, deadline)
                VALUES (?, 'seller-demo', 'buyer-demo', ?, ?, ?)""",
                (
                    transaction_id,
                    amount,
                    timestamp(notice_published_at),
                    timestamp(DeadlinePolicy.deadline_of(notice_published_at)),
                ),
            )
            self.event(
                db,
                transaction_id,
                notice_published_at,
                "반품 승인 안내 게시 · 판매자 지급 보류",
            )
        return transaction_id

    @staticmethod
    def _snapshot(db, transaction_id):
        row = db.execute(
            "SELECT * FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()
        if row is None:
            raise DomainError("거래를 찾을 수 없습니다.", 404)
        value = dict(row)
        value["receipts"] = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM receipts WHERE transaction_id=? ORDER BY received_at, rowid",
                (transaction_id,),
            )
        ]
        value["operations"] = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM operations WHERE transaction_id=?", (transaction_id,)
            )
        ]
        value["events"] = [
            dict(r)
            for r in db.execute(
                "SELECT at, message FROM events WHERE transaction_id=? ORDER BY sequence",
                (transaction_id,),
            )
        ]
        return value

    def get(self, transaction_id):
        with self.connection() as db:
            return self._snapshot(db, transaction_id)

    def list_ids(self):
        with self.connection() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT id FROM transactions ORDER BY rowid DESC LIMIT 100"
                )
            ]

    def accept_waybill(self, transaction_id, actor_id, request_id, waybill_no, clock):
        # The authoritative receipt is the durable DB acceptance. No client timestamp.
        with self.connection(write=True) as db:
            transaction = self._snapshot(db, transaction_id)
            if actor_id != transaction["buyer_id"]:
                raise DomainError("구매자만 반품 운송장을 접수할 수 있습니다.", 403)
            previous = next(
                (r for r in transaction["receipts"] if r["request_id"] == request_id),
                None,
            )
            if previous:
                if previous["waybill_no"] != waybill_no:
                    raise DomainError(
                        "같은 요청 ID에 다른 운송장을 사용할 수 없습니다."
                    )
                return previous["id"]
            now = clock()
            if not DeadlinePolicy.is_before_deadline(
                now, instant(transaction["deadline"])
            ):
                raise DomainError("반품 운송장 등록 기한이 지났습니다.")
            if transaction["operations"]:
                raise DomainError("이미 지급 또는 환불이 결정된 거래입니다.")
            receipt_id = str(uuid4())
            db.execute(
                """INSERT INTO receipts
                (id, transaction_id, request_id, waybill_no, received_at, status, next_check_at)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?)""",
                (
                    receipt_id,
                    transaction_id,
                    request_id,
                    waybill_no,
                    timestamp(now),
                    timestamp(now),
                ),
            )
            db.execute(
                "UPDATE transactions SET version=version+1 WHERE id=?",
                (transaction_id,),
            )
            self.event(db, transaction_id, now, "운송장 접수 저장 · 운송장 확인 중")
            return receipt_id

    def claim_once(self, snapshot, decision, now):
        """RequestGuard: version check and instruction are ONE transaction.

        A single principal allocation is represented by UNIQUE(transaction_id).
        PAYOUT and REFUND cannot claim separate keys for the same money.
        """
        with self.connection(write=True) as db:
            current = self._snapshot(db, snapshot["id"])
            if current["version"] != snapshot["version"]:
                return False
            if current["operations"] or decision == Decision.WAIT:
                return True
            if decision == Decision.HOLD:
                if current["hold_decided_at"] is None and now >= instant(
                    current["deadline"]
                ):
                    db.execute(
                        "UPDATE transactions SET hold_decided_at=?, version=version+1 WHERE id=?",
                        (timestamp(now), current["id"]),
                    )
                    self.event(
                        db,
                        current["id"],
                        now,
                        "만료 작업: 기한 내 접수 확인 · 지급 보류 유지",
                    )
                return True
            recipient = (
                current["seller_id"]
                if decision == Decision.PAYOUT
                else current["buyer_id"]
            )
            db.execute(
                """INSERT INTO operations
                (request_key, transaction_id, kind, recipient_id, amount) VALUES (?, ?, ?, ?, ?)""",
                (
                    f"principal:{current['id']}",
                    current["id"],
                    decision,
                    recipient,
                    current["amount"],
                ),
            )
            db.execute(
                "UPDATE transactions SET version=version+1 WHERE id=?", (current["id"],)
            )
            message = (
                "기한 만료 · 판매자 지급 지시"
                if decision == Decision.PAYOUT
                else "반품 도착 확인 · 구매자 환불 지시"
            )
            self.event(db, current["id"], now, message)
            return True

    def find_due_deadlines(self, now):
        with self.connection() as db:
            return [
                r[0]
                for r in db.execute(
                    """SELECT id FROM transactions t WHERE deadline<=?
                AND NOT EXISTS(SELECT 1 FROM operations o WHERE o.transaction_id=t.id)""",
                    (timestamp(now),),
                )
            ]

    def claim_receipt(self, now, lease_seconds=30):
        token = str(uuid4())
        with self.connection(write=True) as db:
            row = db.execute(
                """SELECT r.*, t.seller_id FROM receipts r
                JOIN transactions t ON t.id=r.transaction_id
                WHERE r.status IN ('PENDING', 'UNKNOWN') AND r.next_check_at<=?
                AND (r.lease_until IS NULL OR r.lease_until<=?)
                ORDER BY r.next_check_at LIMIT 1""",
                (timestamp(now), timestamp(now)),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE receipts SET lease_token=?, lease_until=? WHERE id=?",
                (token, timestamp(now + timedelta(seconds=lease_seconds)), row["id"]),
            )
            return {**dict(row), "lease_token": token}

    def finish_receipt(self, receipt, result, now, max_attempts, retry_seconds):
        with self.connection(write=True) as db:
            current = db.execute(
                "SELECT * FROM receipts WHERE id=?", (receipt["id"],)
            ).fetchone()
            # A worker whose lease was taken over cannot overwrite a newer result.
            if current["lease_token"] != receipt["lease_token"]:
                return False
            attempts = current["attempts"] + 1
            if result.valid is None:
                status = (
                    ReceiptStatus.MANUAL_REVIEW
                    if attempts >= max_attempts
                    else ReceiptStatus.UNKNOWN
                )
            elif result.valid and result.recipient_id == receipt["seller_id"]:
                status = (
                    ReceiptStatus.DELIVERED if result.delivered else ReceiptStatus.VALID
                )
            else:
                status = ReceiptStatus.INVALID
            db.execute(
                """UPDATE receipts SET status=?, attempts=?, next_check_at=?,
                lease_token=NULL, lease_until=NULL WHERE id=?""",
                (
                    status,
                    attempts,
                    timestamp(now + timedelta(seconds=retry_seconds)),
                    receipt["id"],
                ),
            )
            db.execute(
                "UPDATE transactions SET version=version+1 WHERE id=?",
                (receipt["transaction_id"],),
            )
            messages = {
                ReceiptStatus.MANUAL_REVIEW: "택배 조회 한도 도달 · 담당자 확인 필요 · 지급 보류",
                ReceiptStatus.UNKNOWN: "택배 응답 없음 · 재조회 대기 · 지급 보류",
                ReceiptStatus.VALID: "운송장과 판매자 수취인 확인 · 반품 진행",
                ReceiptStatus.DELIVERED: "택배사에서 판매자에게 반품 도착 확인",
                ReceiptStatus.INVALID: "운송장 무효 또는 수취인 불일치 · 접수 거절",
            }
            self.event(db, receipt["transaction_id"], now, messages[status])
            return True

    def mark_mock_arrived(self, transaction_id, now):
        with self.connection(write=True) as db:
            transaction = self._snapshot(db, transaction_id)
            if any(
                r["status"] == ReceiptStatus.DELIVERED for r in transaction["receipts"]
            ):
                return
            receipt = next(
                (
                    r
                    for r in transaction["receipts"]
                    if r["status"] == ReceiptStatus.VALID
                ),
                None,
            )
            if receipt is None:
                raise DomainError("유효성이 확인된 반품 운송장이 필요합니다.")
            db.execute(
                "UPDATE receipts SET status='DELIVERED' WHERE id=?", (receipt["id"],)
            )
            db.execute(
                "UPDATE transactions SET version=version+1 WHERE id=?",
                (transaction_id,),
            )
            self.event(
                db, transaction_id, now, "모의 택배사: 판매자에게 반품 도착 확인"
            )

    def pending_operations(self):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM operations WHERE status='PENDING'")
            ]

    def record_mock_transfer(self, operation):
        with self.connection(write=True) as db:
            db.execute(
                """INSERT INTO mock_bank_transfers
                (request_key, transaction_id, kind, recipient_id, amount) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(request_key) DO NOTHING""",
                tuple(
                    operation[k]
                    for k in (
                        "request_key",
                        "transaction_id",
                        "kind",
                        "recipient_id",
                        "amount",
                    )
                ),
            )
            existing = dict(
                db.execute(
                    "SELECT * FROM mock_bank_transfers WHERE request_key=?",
                    (operation["request_key"],),
                ).fetchone()
            )
            if any(existing[k] != operation[k] for k in existing):
                raise DomainError("은행 멱등키의 금전 지시 내용이 다릅니다.")
        return "SUCCEEDED"

    def save_transfer_result(self, request_key, status, now):
        with self.connection(write=True) as db:
            row = db.execute(
                "SELECT * FROM operations WHERE request_key=?", (request_key,)
            ).fetchone()
            if row and row["status"] != status:
                db.execute(
                    "UPDATE operations SET status=? WHERE request_key=?",
                    (status, request_key),
                )
                db.execute(
                    "UPDATE transactions SET version=version+1 WHERE id=?",
                    (row["transaction_id"],),
                )
                self.event(
                    db,
                    row["transaction_id"],
                    now,
                    "모의 은행: 판매자 지급 완료"
                    if row["kind"] == "PAYOUT"
                    else "모의 은행: 구매자 환불 완료",
                )

    def transfer_count(self, kind):
        with self.connection() as db:
            return db.execute(
                "SELECT COUNT(*) FROM mock_bank_transfers WHERE kind=?", (kind,)
            ).fetchone()[0]
