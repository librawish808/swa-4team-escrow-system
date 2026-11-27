"""택배 API 호출 담당 (통합 DCD의 DeliveryApiClient)."""
from __future__ import annotations

from .mock_courier import MockCourierServer


class DeliveryApiClient:
    def __init__(self, server: MockCourierServer, timeout_millis: int = 3000) -> None:
        self.server = server
        self.timeout_millis = timeout_millis

    def query_shipment(self, waybill_no: str) -> dict:
        return self.server.query_shipment(waybill_no)
