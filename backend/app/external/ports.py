"""B(거래 처리)가 호출하는 외부 연동 인터페이스 (통합 DCD의 BankPort, DeliveryPort)."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .results import DepositCheckResult, ShipmentResult, TransferResult


class BankPort(ABC):
    @abstractmethod
    def check_deposit(self, transaction_id: str) -> DepositCheckResult:
        """구매자 입금 여부 조회."""

    @abstractmethod
    def request_payout(self, transaction_id: str, request_key: str) -> TransferResult:
        """판매자 지급 요청. 같은 request_key는 은행에서 한 번만 처리된다."""

    @abstractmethod
    def request_refund(self, transaction_id: str, request_key: str) -> TransferResult:
        """구매자 환불 요청. 같은 request_key는 은행에서 한 번만 처리된다."""

    @abstractmethod
    def get_transfer_result(self, request_key: str) -> TransferResult:
        """기존 지급·환불 요청의 결과 조회. 새 이체를 만들지 않는다."""


class DeliveryPort(ABC):
    @abstractmethod
    def register_waybill(self, transaction_id: str, waybill_no: str) -> ShipmentResult:
        """운송장 유효성 확인 후 등록 (택배사 조회로 확인)."""

    @abstractmethod
    def get_shipment_status(self, transaction_id: str, waybill_no: str) -> ShipmentResult:
        """배송 상태·수취인·배송 완료 시각 조회."""
