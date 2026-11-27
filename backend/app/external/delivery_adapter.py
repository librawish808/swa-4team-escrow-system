"""DeliveryPort 구현 (통합 DCD의 DeliveryAdapter). 조회만 하므로 모두 재시도 가능."""
from __future__ import annotations

import logging
from typing import Optional

from .delivery_client import DeliveryApiClient
from .delivery_mapper import DeliveryResponseMapper
from .errors import ExternalCallError
from .ports import DeliveryPort
from .results import LookupStatus, ShipmentResult

log = logging.getLogger(__name__)


class DeliveryAdapter(DeliveryPort):
    def __init__(
        self,
        client: DeliveryApiClient,
        mapper: Optional[DeliveryResponseMapper] = None,
        lookup_attempts: int = 2,
    ) -> None:
        self.client = client
        self.mapper = mapper or DeliveryResponseMapper()
        self.lookup_attempts = lookup_attempts

    def register_waybill(self, transaction_id: str, waybill_no: str) -> ShipmentResult:
        # 실제 택배 API는 조회만 제공하므로, '등록' = 택배사 조회로 유효성 확인 (팀 합의 T-4)
        return self.get_shipment_status(transaction_id, waybill_no)

    def get_shipment_status(self, transaction_id: str, waybill_no: str) -> ShipmentResult:
        for attempt in range(1, self.lookup_attempts + 1):
            try:
                return self.mapper.to_shipment_result(self.client.query_shipment(waybill_no))
            except ExternalCallError as e:
                log.warning("courier lookup attempt %d/%d failed: %s", attempt, self.lookup_attempts, e)
        return ShipmentResult(status=LookupStatus.UNKNOWN, waybill_no=waybill_no, message="courier unavailable")
