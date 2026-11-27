"""외부 택배 시스템 시뮬레이터 (데모·테스트용). 장애를 주입할 수 있다."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional

from .errors import ExternalConnectionError


class MockCourierServer:
    def __init__(self) -> None:
        self.down = False
        self._transient_failures = 0
        self._shipments: Dict[str, dict] = {}

    def set_down(self, down: bool) -> None:
        self.down = down

    def fail_next(self, times: int) -> None:
        self._transient_failures = times

    # 테스트 준비용: 택배사에 실제 존재하는 운송장
    def add_shipment(self, waybill_no: str, recipient: str, delivered_at: Optional[datetime] = None) -> None:
        self._shipments[waybill_no] = {"recipient": recipient, "deliveredAt": delivered_at}

    def mark_delivered(self, waybill_no: str) -> None:
        self._shipments[waybill_no]["deliveredAt"] = datetime.now(timezone.utc)

    def query_shipment(self, waybill_no: str) -> dict:
        if self._transient_failures > 0:
            self._transient_failures -= 1
            raise ExternalConnectionError("courier: connection refused (transient)")
        if self.down:
            raise ExternalConnectionError("courier: connection refused")
        s = self._shipments.get(waybill_no)
        if s is None:
            return {"found": False, "waybillNo": waybill_no}
        delivered = s["deliveredAt"].isoformat() if s["deliveredAt"] else None
        return {"found": True, "waybillNo": waybill_no, "recipient": s["recipient"], "deliveredAt": delivered}
