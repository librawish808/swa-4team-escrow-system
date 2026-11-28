"""TransactionPolicy 인터페이스와 최소 구현 (통합 DCD의 TransactionPolicy)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..transaction.models import Transaction, TransactionStatus as S


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""


ALLOW = PolicyDecision(True)


def deny(reason: str) -> PolicyDecision:
    return PolicyDecision(False, reason)


class TransactionPolicy(ABC):
    @abstractmethod
    def validate_join(self, tx: Transaction, buyer_id: str, token: str) -> PolicyDecision: ...
    @abstractmethod
    def validate_waybill(self, tx: Transaction, seller_id: str) -> PolicyDecision: ...
    @abstractmethod
    def validate_purchase(self, tx: Transaction, buyer_id: str) -> PolicyDecision: ...
    @abstractmethod
    def validate_cancellation(self, tx: Transaction, actor_id: str) -> PolicyDecision: ...
    @abstractmethod
    def validate_read(self, tx: Transaction, actor_id: str) -> PolicyDecision: ...


class SimpleTransactionPolicy(TransactionPolicy):
    """상태·권한만 검사하는 최소 정책. 기한, 반품, 초과 입금 등은 C 구현에 맡긴다."""

    def validate_join(self, tx, buyer_id, token):
        if tx.status != S.WAITING_BUYER:
            return deny("이미 구매자가 참여한 거래")
        if token != tx.invite_token:
            return deny("초대 토큰 불일치")
        if buyer_id == tx.seller_id:
            return deny("판매자는 구매자로 참여할 수 없음")
        return ALLOW

    def validate_waybill(self, tx, seller_id):
        if seller_id != tx.seller_id:
            return deny("판매자만 운송장을 등록할 수 있음")
        if tx.status != S.DEPOSIT_CONFIRMED:
            return deny("입금 확인 후에만 운송장 등록 가능")
        return ALLOW

    def validate_purchase(self, tx, buyer_id):
        if buyer_id != tx.buyer_id:
            return deny("구매자만 구매확정할 수 있음")
        if tx.status not in (S.WAYBILL_REGISTERED, S.PAYOUT_PROCESSING, S.COMPLETED):
            return deny("운송장 등록 후에만 구매확정 가능")
        return ALLOW  # 처리 중·완료 상태의 재요청은 RequestGuard가 중복으로 걸러 낸다

    def validate_cancellation(self, tx, actor_id):
        if actor_id not in (tx.seller_id, tx.buyer_id):
            return deny("거래 참여자만 취소할 수 있음")
        if tx.status not in (S.WAITING_BUYER, S.WAITING_DEPOSIT, S.DEPOSIT_CONFIRMED, S.REFUND_PROCESSING):
            return deny("운송장 등록 후에는 취소할 수 없음")
        return ALLOW

    def validate_read(self, tx, actor_id):
        if actor_id not in (tx.seller_id, tx.buyer_id):
            return deny("거래 참여자만 조회할 수 있음")
        return ALLOW
