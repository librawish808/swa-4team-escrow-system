"""택배 응답 → 내부 결과 타입 변환 (통합 DCD의 DeliveryResponseMapper)."""
from __future__ import annotations

from datetime import datetime

from .results import LookupStatus, ShipmentResult


class DeliveryResponseMapper:
    def to_shipment_result(self, response: dict) -> ShipmentResult:
        if not response.get("found"):
            return ShipmentResult(status=LookupStatus.NOT_FOUND, waybill_no=response.get("waybillNo"))
        delivered = response.get("deliveredAt")
        return ShipmentResult(
            status=LookupStatus.FOUND,
            waybill_no=response["waybillNo"],
            recipient=response.get("recipient"),
            delivered_at=datetime.fromisoformat(delivered) if delivered else None,
        )
