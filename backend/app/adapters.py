"""Explicit demo adapters. No real bank or courier is called."""

import asyncio

from .domain import ShipmentResult
from .repository import SqlTransactionRepository


class MockDeliveryPort:
    def __init__(self, delay_seconds=3.0):
        self.delay_seconds = delay_seconds

    async def get_shipment_status(self, waybill_no, seller_id):
        await asyncio.sleep(self.delay_seconds)
        if waybill_no.startswith("TIMEOUT"):
            return ShipmentResult(None)
        if waybill_no.startswith("WRONG"):
            return ShipmentResult(True, "another-recipient")
        if waybill_no.startswith("VALID"):
            return ShipmentResult(True, seller_id)
        return ShipmentResult(False)


class MockBankPort:
    def __init__(self, repository: SqlTransactionRepository):
        self.repository = repository

    async def transfer(self, operation):
        # Durable ledger and idempotency simulate the bank's atomic key contract.
        return await asyncio.to_thread(self.repository.record_mock_transfer, operation)
