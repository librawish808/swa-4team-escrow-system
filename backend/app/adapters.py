"""Explicit demo adapters implementing integrated DCD BankPort and DeliveryPort."""

import asyncio

from .domain import (
    DepositCheckResult,
    LookupStatus,
    ShipmentResult,
    TransferResult,
    TransferStatus,
)
from .repository import SqlTransactionRepository


class MockDeliveryPort:
    def __init__(self, delay_seconds=3.0):
        self.delay_seconds = delay_seconds

    async def register_waybill(
        self, transaction_id: str, waybill_no: str
    ) -> ShipmentResult:
        return ShipmentResult(
            valid=True,
            status=LookupStatus.FOUND,
            waybill_no=waybill_no,
        )

    async def get_shipment_status(
        self, waybill_no: str, seller_id: str
    ) -> ShipmentResult:
        await asyncio.sleep(self.delay_seconds)
        if waybill_no.startswith("TIMEOUT"):
            return ShipmentResult(None, waybill_no=waybill_no)
        if waybill_no.startswith("WRONG"):
            return ShipmentResult(True, "another-recipient", waybill_no=waybill_no)
        if waybill_no.startswith("VALID"):
            return ShipmentResult(True, seller_id, waybill_no=waybill_no)
        return ShipmentResult(False, waybill_no=waybill_no)


class MockBankPort:
    def __init__(self, repository: SqlTransactionRepository):
        self.repository = repository

    async def check_deposit(self, transaction_id: str) -> DepositCheckResult:
        return DepositCheckResult(status=LookupStatus.NOT_FOUND)

    async def request_payout(
        self, transaction_id: str, request_key: str, operation: dict | None = None
    ) -> TransferResult:
        payload = operation or {
            "request_key": request_key,
            "transaction_id": transaction_id,
            "kind": "PAYOUT",
            "recipient_id": "seller-demo",
            "amount": 0,
        }
        status = await asyncio.to_thread(self.repository.record_mock_transfer, payload)
        return TransferResult(
            request_key=request_key,
            status=TransferStatus(status),
            bank_transaction_id=f"mock-tx:{request_key}",
        )

    async def request_refund(
        self, transaction_id: str, request_key: str, operation: dict | None = None
    ) -> TransferResult:
        payload = operation or {
            "request_key": request_key,
            "transaction_id": transaction_id,
            "kind": "REFUND",
            "recipient_id": "buyer-demo",
            "amount": 0,
        }
        status = await asyncio.to_thread(self.repository.record_mock_transfer, payload)
        return TransferResult(
            request_key=request_key,
            status=TransferStatus(status),
            bank_transaction_id=f"mock-tx:{request_key}",
        )

    async def get_transfer_result(self, request_key: str) -> TransferResult:
        existing = await asyncio.to_thread(
            self.repository.find_mock_transfer, request_key
        )
        if existing is not None:
            return TransferResult(
                request_key=request_key,
                status=TransferStatus.SUCCEEDED,
                bank_transaction_id=f"mock-tx:{request_key}",
            )
        return TransferResult(
            request_key=request_key,
            status=TransferStatus.NOT_FOUND,
        )

    async def transfer(self, operation: dict) -> str:
        # Backward-compatible helper delegating to request_payout / request_refund.
        if operation["kind"] == "PAYOUT":
            result = await self.request_payout(
                operation["transaction_id"], operation["request_key"], operation
            )
        else:
            result = await self.request_refund(
                operation["transaction_id"], operation["request_key"], operation
            )
        return result.status
