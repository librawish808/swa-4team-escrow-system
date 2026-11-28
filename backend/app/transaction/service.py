"""거래 처리 흐름 (통합 DCD의 TransactionService, 최소 구현).

가용성·장애 복구성 QAS가 B 쪽에 요구하는 규칙:
1. 외부 호출 전에 상태(…_PROCESSING)를 먼저 저장한다. 서버가 죽어도 복구할 거래가 남는다.
2. 은행 결과가 SUCCEEDED일 때만 완료 상태로 바꾼다. UNKNOWN이면 처리 중 상태를 유지한다.
3. 외부 장애는 예외가 아니라 결과 코드로 돌려준다. 다른 기능은 계속 동작한다.
4. 처리 중 거래는 recover_processing_transactions()로 은행 결과와 맞춘다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Tuple

from ..external.ports import BankPort, DeliveryPort
from ..external.results import LookupStatus, TransferResult, TransferStatus
from ..policy.policy import TransactionPolicy
from ..policy.request_guard import RequestGuard
from .models import Transaction, TransactionStatus as S
from .repository import ConcurrentModificationError, TransactionRepository

log = logging.getLogger(__name__)

PAYOUT = "PAYOUT"
REFUND = "REFUND"


class TransactionNotFound(Exception):
    pass


@dataclass(frozen=True)
class ServiceResult:
    transaction_id: str
    status: S
    code: str
    message: str = ""


@dataclass
class RecoveryReport:
    checked: int = 0
    completed: int = 0  # 지급 완료 또는 취소(환불) 완료로 확정
    resent: int = 0  # 은행에 기록이 없어(NOT_FOUND) 같은 requestKey로 다시 보냄
    still_processing: int = 0  # 은행이 아직 응답하지 않음(UNKNOWN) → 다음 주기에 재시도
    failed: int = 0  # 은행이 거절(FAILED) → C 정책 결정 전까지 처리 중 유지
    skipped: int = 0  # 동시에 다른 요청이 상태를 바꿈
    details: List[str] = field(default_factory=list)
    outcomes: List[Tuple[str, str, str]] = field(default_factory=list)  # (거래 ID, PAYOUT/REFUND, 결과 코드)


class TransactionService:
    def __init__(
        self,
        repository: TransactionRepository,
        policy: TransactionPolicy,
        guard: RequestGuard,
        bank: BankPort,
        delivery: DeliveryPort,
    ) -> None:
        self.repo = repository
        self.policy = policy
        self.guard = guard
        self.bank = bank
        self.delivery = delivery

    # ---------------- UC1~UC7 ----------------
    def create_transaction(self, seller_id: str, amount: int) -> Transaction:
        tx = Transaction.create(seller_id, amount)
        self.repo.save(tx)
        return tx

    def join_transaction(self, transaction_id: str, buyer_id: str, token: str) -> ServiceResult:
        tx = self._load(transaction_id)
        decision = self.policy.validate_join(tx, buyer_id, token)
        if not decision.allowed:
            return self._result(tx, "REJECTED", decision.reason)
        tx.attach_buyer(buyer_id)
        self.repo.save(tx)
        return self._result(tx, "JOINED")

    def confirm_deposit(self, transaction_id: str) -> ServiceResult:
        tx = self._load(transaction_id)
        if tx.status != S.WAITING_DEPOSIT:
            return self._result(tx, "REJECTED", "입금 대기 상태가 아님")
        check = self.bank.check_deposit(tx.id)
        if check.status == LookupStatus.UNKNOWN:
            return self._result(tx, "BANK_UNAVAILABLE", "은행 응답 없음. 잠시 후 다시 확인하세요")
        if check.status == LookupStatus.NOT_FOUND:
            return self._result(tx, "DEPOSIT_NOT_FOUND", "아직 입금되지 않았습니다")
        if (check.received_amount or 0) < tx.amount:
            return self._result(tx, "DEPOSIT_INSUFFICIENT", "입금액이 거래 금액보다 적습니다")
        tx.mark_deposit_confirmed()
        self.repo.save(tx)
        return self._result(tx, "DEPOSIT_CONFIRMED")

    def register_waybill(self, transaction_id: str, seller_id: str, waybill_no: str) -> ServiceResult:
        tx = self._load(transaction_id)
        decision = self.policy.validate_waybill(tx, seller_id)
        if not decision.allowed:
            return self._result(tx, "REJECTED", decision.reason)
        shipment = self.delivery.register_waybill(tx.id, waybill_no)
        if shipment.status == LookupStatus.UNKNOWN:
            return self._result(tx, "COURIER_UNAVAILABLE", "택배 시스템 응답 없음. 잠시 후 다시 등록하세요")
        if shipment.status == LookupStatus.NOT_FOUND:
            return self._result(tx, "WAYBILL_INVALID", "택배사에 없는 운송장 번호입니다")
        tx.mark_waybill_registered(waybill_no)
        self.repo.save(tx)
        return self._result(tx, "WAYBILL_REGISTERED")

    def confirm_purchase(self, transaction_id: str, buyer_id: str) -> ServiceResult:
        tx = self._load(transaction_id)
        decision = self.policy.validate_purchase(tx, buyer_id)
        if not decision.allowed:
            return self._result(tx, "REJECTED", decision.reason)
        if not self.guard.claim_once(tx.id, PAYOUT):
            return self._result(tx, "DUPLICATE_REQUEST", "이미 지급을 요청한 거래입니다")
        tx.start_payout()
        self.repo.save(tx)  # 외부 호출 전 상태 저장
        result = self.bank.request_payout(tx.id, tx.payout_request_key)
        return self._apply_transfer_result(tx, result)

    def cancel_transaction(self, transaction_id: str, actor_id: str) -> ServiceResult:
        tx = self._load(transaction_id)
        decision = self.policy.validate_cancellation(tx, actor_id)
        if not decision.allowed:
            return self._result(tx, "REJECTED", decision.reason)
        if tx.status in (S.WAITING_BUYER, S.WAITING_DEPOSIT):
            tx.cancel_before_deposit()
            self.repo.save(tx)
            return self._result(tx, "CANCELLED", "입금 전 취소 (환불 없음)")
        if not self.guard.claim_once(tx.id, REFUND):
            return self._result(tx, "DUPLICATE_REQUEST", "이미 환불을 요청한 거래입니다")
        tx.start_refund()
        self.repo.save(tx)  # 외부 호출 전 상태 저장
        result = self.bank.request_refund(tx.id, tx.refund_request_key)
        return self._apply_transfer_result(tx, result)

    def get_transaction_status(self, transaction_id: str, actor_id: str) -> ServiceResult:
        tx = self._load(transaction_id)
        decision = self.policy.validate_read(tx, actor_id)
        if not decision.allowed:
            return ServiceResult(tx.id, tx.status, "REJECTED", decision.reason)
        return self._result(tx, "OK")

    # ---------------- 장애 복구 (QAS-AV-01 ⑤, QAS-AV-02) ----------------
    def recover_processing_transactions(self) -> RecoveryReport:
        """처리 중인 지급·환불을 은행 기록과 맞춘다. ExpirationJob이 주기적으로 호출한다."""
        report = RecoveryReport()
        for tx in self.repo.find_by_status([S.PAYOUT_PROCESSING, S.REFUND_PROCESSING]):
            report.checked += 1
            is_payout = tx.status == S.PAYOUT_PROCESSING
            key = tx.payout_request_key if is_payout else tx.refund_request_key

            result = self.bank.get_transfer_result(key)
            if result.status == TransferStatus.NOT_FOUND:
                # 은행이 요청을 받지 못함 (예: 호출 직전 서버 종료) → 같은 키로 다시 요청
                send = self.bank.request_payout if is_payout else self.bank.request_refund
                result = send(tx.id, key)
                report.resent += 1

            try:
                outcome = self._apply_transfer_result(tx, result)
            except ConcurrentModificationError:
                report.skipped += 1
                continue

            if outcome.code in ("COMPLETED", "CANCELLED"):
                report.completed += 1
            elif outcome.code == "TRANSFER_FAILED":
                report.failed += 1
            else:
                report.still_processing += 1
            kind = PAYOUT if is_payout else REFUND
            report.details.append(f"{tx.id[:8]} {kind} -> {outcome.code}")
            report.outcomes.append((tx.id, kind, outcome.code))
        return report

    # ---------------- 내부 ----------------
    def _apply_transfer_result(self, tx: Transaction, result: TransferResult) -> ServiceResult:
        if result.status == TransferStatus.SUCCEEDED:
            if tx.status == S.PAYOUT_PROCESSING:
                tx.complete_payout()
                self.repo.save(tx)
                return self._result(tx, "COMPLETED")
            tx.complete_cancellation()
            self.repo.save(tx)
            return self._result(tx, "CANCELLED", "환불 완료")
        if result.status == TransferStatus.FAILED:
            # 거절 후속 처리는 C 정책 결정 대기 → 일단 처리 중 상태 유지
            log.error("transfer rejected: %s", result.request_key)
            return self._result(tx, "TRANSFER_FAILED", "은행이 이체를 거절했습니다. 관리자 확인 필요")
        # UNKNOWN (또는 재요청 후에도 NOT_FOUND): 완료로 표시하지 않음
        return self._result(tx, "PROCESSING", "은행 처리 결과 확인 중입니다")

    def _load(self, transaction_id: str) -> Transaction:
        tx = self.repo.find_by_id(transaction_id)
        if tx is None:
            raise TransactionNotFound(transaction_id)
        return tx

    @staticmethod
    def _result(tx: Transaction, code: str, message: str = "") -> ServiceResult:
        return ServiceResult(tx.id, tx.status, code, message)
