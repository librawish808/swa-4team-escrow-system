"""구성 조립 (Mock 은행·택배 + SQLite). 테스트·데모·이후 FastAPI 라우터에서 공통으로 쓴다."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .external.bank_adapter import BankAdapter
from .external.bank_client import BankApiClient
from .external.delivery_adapter import DeliveryAdapter
from .external.delivery_client import DeliveryApiClient
from .external.mock_bank import MockBankServer
from .external.mock_courier import MockCourierServer
from .external.ports import BankPort
from .policy.policy import SimpleTransactionPolicy
from .policy.request_guard import SqliteRequestGuard
from .recovery.expiration_job import ExpirationJob
from .transaction.repository import SqliteTransactionRepository
from .transaction.service import TransactionService


@dataclass
class EscrowSystem:
    service: TransactionService
    job: ExpirationJob
    repository: SqliteTransactionRepository
    guard: SqliteRequestGuard
    bank_server: MockBankServer
    courier_server: MockCourierServer

    def shutdown(self) -> None:
        """서버 프로세스 종료를 흉내 낸다 (메모리 상태는 사라지고 DB 파일만 남음)."""
        self.job.stop()
        self.repository.close()
        self.guard.close()


def build_system(
    db_path: str = ":memory:",
    bank_server: Optional[MockBankServer] = None,
    courier_server: Optional[MockCourierServer] = None,
    timeout_millis: int = 3000,
    lookup_attempts: int = 2,
    bank_port: Optional[BankPort] = None,
    job_interval_seconds: float = 60.0,
) -> EscrowSystem:
    bank_server = bank_server or MockBankServer()
    courier_server = courier_server or MockCourierServer()
    bank = bank_port or BankAdapter(BankApiClient(bank_server, timeout_millis), lookup_attempts=lookup_attempts)
    delivery = DeliveryAdapter(DeliveryApiClient(courier_server, timeout_millis), lookup_attempts=lookup_attempts)
    repo = SqliteTransactionRepository(db_path)
    guard = SqliteRequestGuard(db_path)
    service = TransactionService(repo, SimpleTransactionPolicy(), guard, bank, delivery)
    job = ExpirationJob(service, job_interval_seconds)
    return EscrowSystem(service, job, repo, guard, bank_server, courier_server)
