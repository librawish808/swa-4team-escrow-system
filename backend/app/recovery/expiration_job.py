"""ExpirationJob: 서버 시작 직후 1회 + 주기적으로 처리 중 거래를 복구한다.

지금은 장애 복구(은행 결과 재확인)만 한다.
자동 구매확정, 반품 마감 같은 기한 만료 판단은 B·C 구현과 합칠 때 추가한다.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from ..transaction.service import RecoveryReport, TransactionService

log = logging.getLogger(__name__)


class ExpirationJob:
    def __init__(self, service: TransactionService, interval_seconds: float = 60.0) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_run_at: Optional[datetime] = None
        self.last_report: Optional[RecoveryReport] = None
        self.on_report: Optional[Callable[[RecoveryReport], None]] = None  # 데모 화면 기록용

    def run(self) -> RecoveryReport:
        report = self.service.recover_processing_transactions()
        self.last_run_at = datetime.now(timezone.utc)
        self.last_report = report
        if self.on_report:
            self.on_report(report)
        if report.checked:
            log.info(
                "recovery: checked=%d completed=%d resent=%d processing=%d failed=%d",
                report.checked, report.completed, report.resent, report.still_processing, report.failed,
            )
        return report

    def start(self) -> None:
        """서버 시작 시 호출: 즉시 1회 실행 후 interval마다 반복."""
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.run()
                except Exception:  # 복구 작업이 죽어도 서버는 계속 동작해야 한다
                    log.exception("recovery job failed")
                self._stop.wait(self.interval_seconds)

        self._thread = threading.Thread(target=loop, name="expiration-job", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
